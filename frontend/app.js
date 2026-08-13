// Browser client: microphone in, speaker out, transcripts on screen.
//
// This file holds no credentials, no Azure endpoint, and no knowledge of the RFP
// corpus. It talks only to our own backend over one WebSocket:
//   -> binary frames: PCM16 microphone audio
//   <- binary frames: PCM16 assistant audio
//   <- text frames  : JSON status, transcripts, tool activity

const SAMPLE_RATE = 24000;

const els = {
  toggle: document.getElementById("toggle"),
  state: document.getElementById("state"),
  route: document.getElementById("route"),
  transcript: document.getElementById("transcript"),
  activity: document.getElementById("activity"),
  orb: document.getElementById("orb"),
};

let socket = null;
let audioContext = null;
let micStream = null;
let captureNode = null;
let playerNode = null;
let running = false;

function setState(text, cls) {
  els.state.textContent = text;
  els.state.className = `state ${cls || ""}`;
}

function addTurn(role, text) {
  if (!text) return;
  const row = document.createElement("div");
  row.className = `turn ${role}`;
  row.innerHTML = `<span class="who">${role === "user" ? "You" : "Iris"}</span><p></p>`;
  row.querySelector("p").textContent = text;
  els.transcript.appendChild(row);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function addActivity(text, kind) {
  const row = document.createElement("div");
  row.className = `activity-row ${kind || ""}`;
  row.textContent = text;
  els.activity.prepend(row);
  while (els.activity.childElementCount > 40) els.activity.lastElementChild.remove();
}

async function start() {
  setState("connecting", "busy");

  // Audio graph and backend socket come up first. If the microphone is then
  // refused, the user sees "microphone blocked" against a working session rather
  // than an unexplained hang.
  audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
  await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${wsProtocol}//${location.host}/ws`);
  socket.binaryType = "arraybuffer";

  playerNode = new AudioWorkletNode(audioContext, "pcm-player", {
    numberOfInputs: 0,
    outputChannelCount: [1],
  });
  playerNode.connect(audioContext.destination);
  playerNode.port.onmessage = ({ data }) => {
    els.orb.classList.toggle("speaking", data.event === "started");
  };

  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      playerNode.port.postMessage({ command: "push", payload: event.data }, [event.data]);
      return;
    }
    handleEvent(JSON.parse(event.data));
  };
  socket.onclose = () => { if (running) stop(); };
  socket.onerror = () => setState("connection error", "error");

  running = true;
  els.toggle.textContent = "End session";
  els.toggle.classList.add("active");

  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: SAMPLE_RATE,
      // Voice Live also does server-side suppression; browser-side helps the
      // signal that reaches it in the first place.
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  captureNode = new AudioWorkletNode(audioContext, "pcm-capture");
  captureNode.port.onmessage = ({ data }) => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(data.buffer);
  };
  audioContext.createMediaStreamSource(micStream).connect(captureNode);
  addActivity("microphone open");
}

function handleEvent(message) {
  switch (message.type) {
    case "status":
      setState("listening", "ok");
      els.route.textContent =
        `${message.model} · ${message.route === "byom" ? "your deployment (BYOM)" : "Microsoft-hosted"} · ${message.voice}`;
      addActivity(`session ${message.session}`);
      break;

    case "transcript":
      addTurn(message.role, message.text);
      break;

    case "clear":
      // Barge-in: the user started talking, so bin queued assistant audio.
      playerNode.port.postMessage({ command: "clear" });
      els.orb.classList.remove("speaking");
      break;

    case "tool":
      if (message.state === "running") {
        addActivity(`${message.name} → "${message.query}"`, "tool");
        setState("looking it up", "busy");
      } else {
        addActivity(`${message.name} ← ${message.chars} chars`, "tool done");
        setState("listening", "ok");
      }
      break;

    case "error":
      addActivity(message.message, "error");
      setState("error", "error");
      break;
  }
}

function stop() {
  running = false;
  if (socket) { socket.close(); socket = null; }
  if (captureNode) { captureNode.disconnect(); captureNode = null; }
  if (playerNode) { playerNode.disconnect(); playerNode = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }

  els.toggle.textContent = "Start session";
  els.toggle.classList.remove("active");
  els.orb.classList.remove("speaking");
  setState("idle");
  els.route.textContent = "";
}

els.toggle.addEventListener("click", async () => {
  if (running) return stop();
  try {
    await start();
  } catch (err) {
    console.error(err);
    // stop() resets the status line, so set the message after tearing down.
    stop();
    setState(
      err.name === "NotAllowedError" ? "microphone blocked" : "failed to start",
      "error"
    );
    addActivity(`${err.name}: ${err.message}`, "error");
  }
});
