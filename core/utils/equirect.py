"""Equirectangular ↔ perspective projection utilities.

Extracts rectilinear (perspective) views from equirectangular panorama images
at arbitrary heading, pitch, and field-of-view.
"""

from __future__ import annotations

import numpy as np
from PIL import Image
from scipy.ndimage import map_coordinates


def equirect_to_perspective(
    equirect: np.ndarray,
    heading: float,
    pitch: float,
    fov: float,
    out_w: int = 640,
    out_h: int = 640,
) -> np.ndarray:
    """Extract a perspective view from an equirectangular panorama.

    Parameters
    ----------
    equirect : np.ndarray
        H×W×3 equirectangular image (uint8).
    heading : float
        Horizontal viewing direction in degrees (0 = north, 90 = east).
    pitch : float
        Vertical viewing angle in degrees (0 = horizontal, +90 = up, -90 = down).
    fov : float
        Horizontal field of view in degrees (e.g. 90).
    out_w : int
        Output image width in pixels.
    out_h : int
        Output image height in pixels.

    Returns
    -------
    np.ndarray
        out_h×out_w×3 perspective image (uint8).
    """
    eq_h, eq_w = equirect.shape[:2]

    fov_rad = np.radians(fov)
    theta_0 = np.radians(heading)
    # Google Maps convention: positive pitch = look UP.
    # Standard Rx(phi) rotates (0,0,1) to (0,-sin(phi),cos(phi)) = looking DOWN.
    # Negate to match Google's convention: Rx(-pitch) sends forward vector UP.
    phi_0 = np.radians(-pitch)

    # Focal length from horizontal FOV
    f = (out_w / 2.0) / np.tan(fov_rad / 2.0)

    # Pixel grid for output image: camera-space coordinates
    u = np.arange(out_w, dtype=np.float64) - (out_w - 1) / 2.0
    v = (out_h - 1) / 2.0 - np.arange(out_h, dtype=np.float64)
    u, v = np.meshgrid(u, v)

    # Ray directions in camera space (x=right, y=up, z=forward)
    d = np.stack([u, v, np.full_like(u, f)], axis=-1)
    norm = np.linalg.norm(d, axis=-1, keepdims=True)
    d = d / norm

    # Rotation: first pitch (around X), then heading (around Y)
    cos_p, sin_p = np.cos(phi_0), np.sin(phi_0)
    cos_h, sin_h = np.cos(theta_0), np.sin(theta_0)

    # Pitch rotation (around X-axis): tilts up/down
    rx = np.array([
        [1, 0, 0],
        [0, cos_p, -sin_p],
        [0, sin_p, cos_p],
    ])

    # Heading rotation (around Y-axis): rotates left/right
    ry = np.array([
        [cos_h, 0, sin_h],
        [0, 1, 0],
        [-sin_h, 0, cos_h],
    ])

    rot = ry @ rx  # heading then pitch
    d_world = np.einsum("ij,hwj->hwi", rot, d)

    # Spherical coordinates from world-space directions
    x_w = d_world[..., 0]
    y_w = d_world[..., 1]
    z_w = d_world[..., 2]

    # Longitude (theta): angle in XZ plane from +Z axis
    lon = np.arctan2(x_w, z_w)  # [-pi, pi], 0 = north (+Z)
    # Latitude (phi): elevation from XZ plane
    lat = np.arcsin(np.clip(y_w, -1.0, 1.0))  # [-pi/2, pi/2]

    # Map spherical → equirectangular pixel coordinates
    # Longitude: -pi..pi → 0..eq_w
    eq_x = ((lon / np.pi + 1.0) / 2.0) * eq_w
    # Latitude: pi/2..-pi/2 → 0..eq_h  (top of image = north pole)
    eq_y = (0.5 - lat / np.pi) * eq_h

    # Bicubic interpolation via scipy (order=3) — much sharper than bilinear
    eq_x = eq_x % eq_w  # wrap horizontally
    eq_y = np.clip(eq_y, 0, eq_h - 1.001)

    out_channels = []
    for c in range(3):
        channel = equirect[:, :, c].astype(np.float64)
        sampled = map_coordinates(
            channel,
            [eq_y.ravel(), eq_x.ravel()],
            order=3,
            mode='wrap',
        ).reshape(out_h, out_w)
        out_channels.append(sampled)

    out = np.stack(out_channels, axis=-1)
    return np.clip(out, 0, 255).astype(np.uint8)


def perspectives_to_equirect(
    views: list[tuple[np.ndarray, float, float, float]],
    out_w: int = 4096,
    out_h: int = 2048,
    edge_margin: int = 40,
    projection: str = "rectilinear",
) -> np.ndarray:
    """Stitch multiple perspective screenshots into an equirectangular panorama.

    Parameters
    ----------
    views : list of (image, heading, pitch, fov) tuples
        Each entry is a perspective screenshot as H×W×3 uint8 array,
        with the heading (degrees), pitch (degrees), and horizontal FOV (degrees)
        used when the screenshot was taken.
    out_w, out_h : int
        Output equirectangular dimensions.
    edge_margin : int
        Pixels near the edge of each screenshot are down-weighted with a smooth
        falloff over this margin to reduce stitching artifacts.
    projection : str
        Projection model of the input screenshots.
        - "stereographic": conformal stereographic (used by Google Maps JS API)
        - "rectilinear": standard pinhole/perspective projection

    Returns
    -------
    np.ndarray
        out_h×out_w×3 equirectangular image (uint8).
    """
    equirect = np.zeros((out_h, out_w, 3), dtype=np.float64)
    weight = np.zeros((out_h, out_w), dtype=np.float64)

    # Precompute spherical directions for every equirectangular pixel
    eq_u = np.arange(out_w, dtype=np.float64)
    eq_v = np.arange(out_h, dtype=np.float64)
    eq_u, eq_v = np.meshgrid(eq_u, eq_v)

    lon = (eq_u / out_w * 2.0 - 1.0) * np.pi       # [-pi, pi]
    lat = (0.5 - eq_v / out_h) * np.pi              # [pi/2, -pi/2]

    cos_lat = np.cos(lat)
    world_x = np.sin(lon) * cos_lat
    world_y = np.sin(lat)
    world_z = np.cos(lon) * cos_lat
    world_dirs = np.stack([world_x, world_y, world_z], axis=-1)

    for img, heading, pitch, fov in views:
        vh, vw = img.shape[:2]
        img_f = img.astype(np.float64)

        fov_rad = np.radians(fov)
        theta_0 = np.radians(heading)
        # Negate pitch: Google Maps positive pitch = UP, Rx(phi) looks DOWN.
        phi_0 = np.radians(-pitch)

        # Inverse rotation: R^T where R = Ry(heading) @ Rx(-pitch)
        cos_p, sin_p = np.cos(phi_0), np.sin(phi_0)
        cos_h, sin_h = np.cos(theta_0), np.sin(theta_0)

        ry_inv = np.array([
            [cos_h, 0, -sin_h],
            [0, 1, 0],
            [sin_h, 0, cos_h],
        ])
        rx_inv = np.array([
            [1, 0, 0],
            [0, cos_p, sin_p],
            [0, -sin_p, cos_p],
        ])
        rot_inv = rx_inv @ ry_inv

        # Transform world directions to camera space
        cam_dirs = np.einsum("ij,hwj->hwi", rot_inv, world_dirs)

        cam_x = cam_dirs[..., 0]
        cam_y = cam_dirs[..., 1]
        cam_z = cam_dirs[..., 2]

        # Only pixels in front of camera
        in_front = cam_z > 0.01

        # Project camera-space directions to screenshot pixel coordinates
        if projection == "stereographic":
            # Stereographic: r = 2f * tan(theta/2) where theta = angle from axis
            # f_stereo chosen so that half-width maps to half-FOV:
            #   vw/2 = 2 * f_s * tan(fov/4)  =>  f_s = vw / (4 * tan(fov/4))
            f_s = vw / (4.0 * np.tan(fov_rad / 4.0))

            # Angle from optical axis
            rho_3d = np.sqrt(cam_x ** 2 + cam_y ** 2)
            theta_cam = np.arctan2(rho_3d, np.where(in_front, cam_z, 1.0))

            # Stereographic radial distance from image centre
            r_img = 2.0 * f_s * np.tan(theta_cam / 2.0)

            # Avoid division by zero for on-axis rays
            safe_rho = np.where(rho_3d > 1e-10, rho_3d, 1.0)
            u = (vw - 1) / 2.0 + r_img * cam_x / safe_rho
            v = (vh - 1) / 2.0 - r_img * cam_y / safe_rho
        else:
            # Rectilinear (pinhole): u = f * x/z
            f = (vw / 2.0) / np.tan(fov_rad / 2.0)
            safe_z = np.where(in_front, cam_z, 1.0)
            u = f * cam_x / safe_z + (vw - 1) / 2.0
            v = (vh - 1) / 2.0 - f * cam_y / safe_z

        valid = in_front & (u >= 0) & (u < vw - 0.001) & (v >= 0) & (v < vh - 0.001)

        # Edge distance: min distance to any screenshot edge (in pixels)
        edge_dist = np.minimum(
            np.minimum(u, (vw - 1) - u),
            np.minimum(v, (vh - 1) - v),
        )
        # Smooth cosine falloff over edge_margin pixels (1.0 at centre, 0.0 at edge)
        margin = max(edge_margin, 1)
        edge_w = np.where(
            edge_dist >= margin, 1.0,
            0.5 - 0.5 * np.cos(np.pi * np.clip(edge_dist / margin, 0, 1)),
        )

        # Combined weight: cosine from optical axis * edge falloff
        w = np.where(valid, np.clip(cam_z, 0, 1) ** 2 * edge_w, 0.0)

        # Bicubic sample from screenshot (order=3 — much sharper than bilinear)
        u_c = np.clip(u, 0, vw - 1.001)
        v_c = np.clip(v, 0, vh - 1.001)

        coords = [v_c.ravel(), u_c.ravel()]
        sampled = np.empty((out_h, out_w, 3), dtype=np.float64)
        for c in range(3):
            sampled[:, :, c] = map_coordinates(
                img_f[:, :, c], coords, order=3, mode='nearest',
            ).reshape(out_h, out_w)

        w_exp = w[..., np.newaxis]
        valid_mask = valid[..., np.newaxis]
        equirect += sampled * w_exp * valid_mask
        weight += w * valid

    # Normalise weighted blend
    mask = weight > 0
    equirect[mask] /= weight[mask, np.newaxis]

    return np.clip(equirect, 0, 255).astype(np.uint8)


def perspectives_to_perspective(
    views: list[tuple[np.ndarray, float, float, float]],
    heading: float,
    pitch: float,
    fov: float,
    out_w: int = 640,
    out_h: int = 640,
    edge_margin: int = 40,
    projection: str = "rectilinear",
) -> np.ndarray:
    """Render a perspective view directly from screenshots, bypassing equirectangular.

    This eliminates the double-interpolation of screenshot→equirect→perspective,
    giving significantly sharper results (single bicubic resampling).

    Parameters
    ----------
    views : list of (image, heading, pitch, fov) tuples
        Raw perspective screenshots as H×W×3 uint8 arrays.
    heading, pitch, fov : float
        Target view parameters in degrees.
    out_w, out_h : int
        Output image dimensions.
    edge_margin : int
        Edge falloff margin for blending (pixels).
    projection : str
        Projection model of the input screenshots ("rectilinear" or "stereographic").

    Returns
    -------
    np.ndarray
        out_h×out_w×3 perspective image (uint8).
    """
    fov_rad = np.radians(fov)
    theta_0 = np.radians(heading)
    phi_0 = np.radians(-pitch)  # negate for Google Maps convention

    # Output focal length
    f_out = (out_w / 2.0) / np.tan(fov_rad / 2.0)

    # Output pixel grid → camera-space ray directions
    u_out = np.arange(out_w, dtype=np.float64) - (out_w - 1) / 2.0
    v_out = (out_h - 1) / 2.0 - np.arange(out_h, dtype=np.float64)
    u_out, v_out = np.meshgrid(u_out, v_out)

    d = np.stack([u_out, v_out, np.full_like(u_out, f_out)], axis=-1)
    d = d / np.linalg.norm(d, axis=-1, keepdims=True)

    # Rotate to world space
    cos_p, sin_p = np.cos(phi_0), np.sin(phi_0)
    cos_h, sin_h = np.cos(theta_0), np.sin(theta_0)
    rx = np.array([[1, 0, 0], [0, cos_p, -sin_p], [0, sin_p, cos_p]])
    ry = np.array([[cos_h, 0, sin_h], [0, 1, 0], [-sin_h, 0, cos_h]])
    rot = ry @ rx
    world_dirs = np.einsum("ij,hwj->hwi", rot, d)

    # Output center direction for view pre-filtering
    out_center = rot @ np.array([0.0, 0.0, 1.0])
    half_diag = np.radians(fov * 0.75)  # conservative diagonal half-angle

    result = np.zeros((out_h, out_w, 3), dtype=np.float64)
    total_w = np.zeros((out_h, out_w), dtype=np.float64)

    for img, v_heading, v_pitch, v_fov in views:
        # Quick angular distance check — skip views too far away
        v_theta = np.radians(v_heading)
        v_phi_center = np.radians(v_pitch)
        v_center = np.array([
            np.sin(v_theta) * np.cos(v_phi_center),
            np.sin(v_phi_center),
            np.cos(v_theta) * np.cos(v_phi_center),
        ])
        ang_dist = np.arccos(np.clip(np.dot(out_center, v_center), -1.0, 1.0))
        if ang_dist > half_diag + np.radians(v_fov * 0.75):
            continue

        vh, vw = img.shape[:2]
        img_f = img.astype(np.float64)

        v_fov_rad = np.radians(v_fov)
        v_phi = np.radians(-v_pitch)

        cos_vp, sin_vp = np.cos(v_phi), np.sin(v_phi)
        cos_vh, sin_vh = np.cos(v_theta), np.sin(v_theta)

        ry_inv = np.array([[cos_vh, 0, -sin_vh], [0, 1, 0], [sin_vh, 0, cos_vh]])
        rx_inv = np.array([[1, 0, 0], [0, cos_vp, sin_vp], [0, -sin_vp, cos_vp]])
        rot_inv = rx_inv @ ry_inv

        cam_dirs = np.einsum("ij,hwj->hwi", rot_inv, world_dirs)
        cam_x = cam_dirs[..., 0]
        cam_y = cam_dirs[..., 1]
        cam_z = cam_dirs[..., 2]

        in_front = cam_z > 0.01

        if projection == "stereographic":
            f_s = vw / (4.0 * np.tan(v_fov_rad / 4.0))
            rho_3d = np.sqrt(cam_x ** 2 + cam_y ** 2)
            theta_cam = np.arctan2(rho_3d, np.where(in_front, cam_z, 1.0))
            r_img = 2.0 * f_s * np.tan(theta_cam / 2.0)
            safe_rho = np.where(rho_3d > 1e-10, rho_3d, 1.0)
            su = (vw - 1) / 2.0 + r_img * cam_x / safe_rho
            sv = (vh - 1) / 2.0 - r_img * cam_y / safe_rho
        else:
            f_v = (vw / 2.0) / np.tan(v_fov_rad / 2.0)
            safe_z = np.where(in_front, cam_z, 1.0)
            su = f_v * cam_x / safe_z + (vw - 1) / 2.0
            sv = (vh - 1) / 2.0 - f_v * cam_y / safe_z

        valid = in_front & (su >= 0) & (su < vw - 0.001) & (sv >= 0) & (sv < vh - 0.001)

        if not np.any(valid):
            continue

        # Edge falloff
        edge_dist = np.minimum(
            np.minimum(su, (vw - 1) - su),
            np.minimum(sv, (vh - 1) - sv),
        )
        margin = max(edge_margin, 1)
        edge_w = np.where(
            edge_dist >= margin, 1.0,
            0.5 - 0.5 * np.cos(np.pi * np.clip(edge_dist / margin, 0, 1)),
        )

        # Sharper weighting (cam_z^4) to strongly prefer view centres, reducing
        # blend-averaging blur in overlap regions
        w = np.where(valid, np.clip(cam_z, 0, 1) ** 4 * edge_w, 0.0)

        # Bicubic sample from screenshot
        su_c = np.clip(su, 0, vw - 1.001)
        sv_c = np.clip(sv, 0, vh - 1.001)
        coords = [sv_c.ravel(), su_c.ravel()]
        sampled = np.empty((out_h, out_w, 3), dtype=np.float64)
        for c in range(3):
            sampled[:, :, c] = map_coordinates(
                img_f[:, :, c], coords, order=3, mode='nearest',
            ).reshape(out_h, out_w)

        w_exp = w[..., np.newaxis]
        valid_mask = valid[..., np.newaxis]
        result += sampled * w_exp * valid_mask
        total_w += w * valid

    mask = total_w > 0
    result[mask] /= total_w[mask, np.newaxis]

    return np.clip(result, 0, 255).astype(np.uint8)


def perspective_to_bytes(
    perspective: np.ndarray,
    fmt: str = "JPEG",
    quality: int = 95,
) -> bytes:
    """Encode a perspective numpy array to image bytes."""
    from io import BytesIO

    img = Image.fromarray(perspective)
    buf = BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()
