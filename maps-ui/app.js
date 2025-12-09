/* * REVERSE ENGINEERED MAP AGENT (No Street Names)
 * Based on MapCrunch logic found in main.min.js
 */

let panorama;
let svService;

// Core agent state (what your LLM cares about)
const agentState = {
    current_node_id: null,
    gps: { lat: null, lng: null },
    heading: null,
    step_count: 0,
    history: []
};

// Utilities
function normalizeHeading(heading) {
    return (heading % 360 + 360) % 360;
}

/**
 * Build the observation JSON that you'll send to the LLM
 */
function buildObservation() {
    if (!panorama) return null;

    const currentPano = panorama.getPano();
    const links = panorama.getLinks();
    const location = panorama.getLocation();
    const pov = panorama.getPov();

    if (!location || !location.latLng || !links) return null;

    const moves = links.map(link => ({
        heading: normalizeHeading(link.heading),
        next_node_id: link.pano
    }));

    return {
        current_node_id: currentPano,
        gps: {
            lat: location.latLng.lat(),
            lng: location.latLng.lng()
        },
        current_heading: normalizeHeading(pov.heading),
        available_moves: moves,
        meta: {
            step_count: agentState.step_count,
            history: agentState.history
        }
    };
}

/**
 *  Update internal state + UI when panorama or links change
 */
function updateAgentData() {
    const obs = buildObservation();
    if (!obs) return;

    // 1. update internal state
    agentState.current_node_id = obs.current_node_id;
    agentState.gps = obs.gps;
    agentState.heading = obs.current_heading;

    if (
        agentState.history.length === 0 ||
        agentState.history[agentState.history.length - 1] !== obs.current_node_id
    ) {
        agentState.history.push(obs.current_node_id);
    }

    // 2. Render JSON to textarea if exists
    const outEl = document.getElementById('ai-output');
    if (outEl) {
        outEl.value = JSON.stringify(obs, null, 2);
    }

    // 3. Render move buttons
    const movesEl = document.getElementById('moves-list');
    if (movesEl) {
        renderMoveButtons(obs.available_moves);
    }

    // 4. Fire a custom event for external listeners
    window.dispatchEvent(new CustomEvent('agent_observation_updated', { detail: obs }));
}

function renderMoveButtons(moves) {
    const list = document.getElementById('moves-list');
    if (!list) return;

    list.innerHTML = '';

    if (!moves || moves.length === 0) {
        list.innerHTML = '<div style="color:red">Dead End. Teleport required.</div>';
        return;
    }

    // Sort moves by heading so buttons appear in a logical clockwise order
    moves.sort((a, b) => a.heading - b.heading);

    moves.forEach(move => {
        const btn = document.createElement('button');
        btn.className = 'move-btn';
        const arrow = getArrowIcon(move.heading);

        btn.innerHTML = `<span class="arrow-icon">${arrow}</span> <b>${Math.round(move.heading)} deg</b>`;

        btn.onclick = () => {
            executeAgentMove(move.next_node_id, move.heading);
        };
        list.appendChild(btn);
    });
}

function executeAgentMove(panoId, heading) {
    if (!panorama) return;

    panorama.setPano(panoId);
    panorama.setPov({
        heading: normalizeHeading(heading),
        pitch: 0
    });
}

function getArrowIcon(heading) {
    // Simple ASCII arrows for the 8 compass directions
    const directions = ['^', 'NE', '>', 'SE', 'v', 'SW', '<', 'NW'];
    // Adjust so 0 (North) maps to index 0
    const index = Math.round(normalizeHeading(heading) / 45) % 8;
    return directions[index];
}

/**
 * TELEPORT LOGIC
 */
function teleportRandomly() {
    // const lat = (Math.random() * 180) - 90;
    // const lng = (Math.random() * 360) - 180;
    // const randomLocation = { lat: lat, lng: lng };
    const radius = 500;
    const knownLocation = { lat: 37.7749, lng: -122.4194 };

    console.log('Agent attempting to teleport...');

    svService.getPanorama({ location: knownLocation, radius: radius }, (data, status) => {
        if (status === google.maps.StreetViewStatus.OK) {
            panorama.setPano(data.location.pano);
            panorama.setPov({ heading: 0, pitch: 0 });

            agentState.step_count = 0;
            agentState.history = [data.location.pano];
        } else {
            console.log('Zero results, retrying...');
            // Optionally: you can call teleportRandomly() again here with some guard
        }
    });
}

/**
 *  PUBLIC API
 * Expose on window.AgentEnv so your python//LLM side knows what to call.
 */
const AgentEnv = {
    /**
     * Initialize the agent environment with a given Street View panorama
     * options = {
     *   panoContainerId: 'string' (HTML element ID for panorama),
     *   randomButtonId?: string (HTML element ID for random teleport button)
     * }
     */
    init(options) {
        const panoId = options?.panoContainerId || 'pano';
        const randomBtnId = options?.randomButtonId || 'teleport-random-btn';

        // 1. Init street view service
        svService = new google.maps.StreetViewService();

        // 2. Init street view panorama
        panorama = new google.maps.StreetViewPanorama(
            document.getElementById(panoId),
            {
                pov: {
                    heading: 34,
                    pitch: 10
                },

                // UI controls customization
                addressControl: true,
                showRoadLabels: false,
                linksControl: true,
                enableCloseButton: false,
                panControl: true,
                clickToGo: true,
                zoom: 1
            }
        );

        // 3. Listeners to keep state + UI in sync
        panorama.addListener('pano_changed', updateAgentData);
        panorama.addListener('links_changed', updateAgentData);
        panorama.addListener('pov_changed', updateAgentData);

        // 4. Random button if exists
        const randomBtn = document.getElementById(randomBtnId);
        if (randomBtn) {
            randomBtn.addEventListener('click', () => {
                this.teleportRandom();
            });
        }

        // 5. Start with a random teleport
        this.teleportRandom();
    },

    /**
     * Get the current observation JSON
     */
    getObservation() {
        return buildObservation();
    },

    /**
     * Teleport to a random location
     */
    teleportRandom() {
        teleportRandomly();
    },

    /**
     * Move to a specified pano ID
     * This corresponds to selecting an item from available_moves
     */
    moveToPano(panoId, heading = null) {
        if (!panorama) return;

        agentState.step_count += 1;

        panorama.setPano(panoId);

        const pov = panorama.getPov();
        panorama.setPov({
            heading: heading !== null ? normalizeHeading(heading) : normalizeHeading(pov.heading),
            pitch: 0
        });
    }
};

window.AgentEnv = AgentEnv;

// Config
const API_BASE = 'http://localhost:8000';

// Call backend
async function sendStep(observation) {
    const res = await fetch(`${API_BASE}/step`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(observation)
    });

    if (!res.ok) throw new Error(`API error: ${res.status}`);

    return res.json();
}


// Render result
function renderStep(result) {
    const panoImgEl = document.getElementById('pano-img');
    if (panoImgEl && result.image_base64) {
        panoImgEl.src = `data:image/jpeg;base64,${result.image_base64}`;
    }

    const actionTextEl = document.getElementById('action-text');
    if (actionTextEl) {
        actionTextEl.textContent = JSON.stringify(result, null, 2);
    }

    // const panoJsonEl = document.getElementById('pano-json');
    // if (panoJsonEl && result.new_state) {
    //     panoJsonEl.textContent = JSON.stringify(result.new_state, null, 2);
    // }
}

// Wire the "send step" button
const sendStepBtn = document.getElementById('send-step-btn');
if (sendStepBtn) {
    sendStepBtn.addEventListener('click', async () => {
        try {
            const obs = AgentEnv.getObservation();
            if (!obs) {
                alert('No observation available yet.');
                return;
            }
            const result = await sendStep(obs);
            renderStep(result);
        } catch (error) {
            console.error(error);
            alert(`Error: ${error.message}`);
        }
    });
}
