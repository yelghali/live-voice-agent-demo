# Which model does a Foundry agent use in voice mode, and what can you control?

Answers below are from running [`scripts/probe_model_control.py`](../scripts/probe_model_control.py)
and [`scripts/probe_voice_matrix.py`](../scripts/probe_voice_matrix.py) against
`fdy-sa33b5nih2ogs` (project `proj-chatbot-gr`, francecentral) on 13 August 2026,
not from reading the documentation.

---

## Short answer

**Voice Live agent mode does not let you choose the model.** The agent's own chat
deployment is the brain, fixed at the moment you create the agent version. Audio is
always **cascaded** — Azure Speech to text, then that chat model, then Azure text to
speech. It is not native speech-to-speech.

Your `gpt-realtime-1.5` deployment therefore **cannot back a Foundry agent**. It is
reachable only in *direct model* mode, where there is no server-side agent at all.

The `model` and `profile` query parameters are **accepted and silently discarded**
in agent mode, which makes this easy to get wrong: the session connects, so it looks
like it worked.

---

## The three models in every Voice Live session

Voice Live is not one model. It is three, chosen independently.

| Layer | Set by | Agent mode | Direct model mode |
|---|---|---|---|
| **Brain** (LLM) | `PromptAgentDefinition(model=...)` / `?model=` | Agent's chat deployment. **No client override.** | `?model=` |
| **Ears** (STT) | `session.input_audio_transcription.model` | `azure-speech` or `mai-transcribe` | plus `whisper-1`, `gpt-4o-transcribe`, … when the brain is a realtime model |
| **Mouth** (TTS) | `session.voice` | Azure voices only | Azure voices, or the model's native voice |

The Foundry portal's **"Generative AI Model"** dropdown that shows *GPT Realtime 1.5*
belongs to the **direct-model** playground (`Azure-Speech-Voice-Live/playground`).
It is not the agent playground. In the agent playground the voice pane exposes only
voice, VAD, temperature and speed — because the model is not yours to pick there.

---

## Evidence

### Agent mode ignores `?model=` and `?profile=`

A session connecting proves nothing on its own, so each real experiment has a
deliberately invalid control alongside it.

| # | Parameters | Result | Reported voice |
|---|---|---|---|
| 1 | `model=gpt-realtime-1.5` | connected | `alloy` |
| 2 | `model=gpt-realtime-1.5` + `profile=byom-azure-openai-realtime` | connected | `alloy` |
| 3 | `agent_name` + `project_name` | connected | `en-US-AvaMultilingualNeural` |
| 4 | agent + `model=gpt-realtime-1.5` | connected | `en-US-AvaMultilingualNeural` |
| 5 | agent + `model` + `profile=byom-...` | connected | `en-US-AvaMultilingualNeural` |
| **6** | agent + **`model=this-model-does-not-exist-xyz`** | **connected** | `en-US-AvaMultilingualNeural` |
| **7** | agent + **`profile=byom-not-a-real-profile-xyz`** | **connected** | `en-US-AvaMultilingualNeural` |
| **8** | agent + voice `{type: azure-realtime-native}` | **rejected** — `Only Azure voice is supported` | — |

Read rows 6 and 7 first. A model name that cannot exist connects happily, and so does
a BYOM profile that cannot exist. Both parameters are being dropped. That makes rows
4 and 5 false positives — they did not demonstrate control, they demonstrated that
the service ignored the request.

Row 8 independently confirms the audio path. `azure-realtime-native` voices only work
when a native speech-to-speech model is generating the audio. Agent mode rejects them
with the same error a plain `gpt-4o-mini` session gives, which places agent mode
firmly on the cascaded Azure TTS path.

The reported voice corroborates it: direct realtime sessions come back as `alloy`
(the model's own voice), while every agent session comes back as the Azure TTS voice
stored in the agent's metadata.

### Your own deployment does work — in direct-model mode

Experiment 2 succeeded. `profile=byom-azure-openai-realtime&model=gpt-realtime-1.5`
routes to *your* Data Zone Standard deployment, so you keep EU processing, your
content-filter configuration, and your quota.

No extra role assignment was needed. The docs require granting the resource's
managed identity `Foundry User` for `byom-azure-openai-chat-completion` and
`byom-foundry-anthropic-messages`; the realtime profile did not need it here, and the
resource already had a system-assigned identity.

### What you *can* control in agent mode

Everything except the brain, via the agent metadata key
`microsoft.voice-live.configuration` (chunked at 512 characters per value — see
`chunk_config` in [`agent/_common.py`](../agent/_common.py)). Verified round-tripping
intact by `agent/create_rfp_agent.py`:

```json
{"session": {
  "voice": {"name": "en-US-AvaMultilingualNeural", "type": "azure-standard"},
  "input_audio_transcription": {"model": "azure-speech"},
  "turn_detection": {"type": "azure_semantic_vad_multilingual",
                     "remove_filler_words": true, "auto_truncate": true},
  "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
  "input_audio_echo_cancellation": {"type": "server_echo_cancellation"}}}
```

To change the brain you create a **new agent version** with a different `model`.
That is the only lever, and `agent_version` is how you pin it.

### How to see what a live session resolved to

`session.updated` reports the agent and the voice. It does **not** report the LLM
deployment name — pin `agent_version` if you need that to be deterministic.

```
SessionID       : sess_1iu0UIOmse9nxQES8aIoUk
Agent Name      : rfp-voice-agent
Voice Name      : en-US-AvaMultilingualNeural
Voice Type      : azure-standard
```

`agent/voice_live_agent_client.py` writes this to `logs/<timestamp>_conversation.log`
on every run.

---

## So what *is* "the voice model" in agent mode?

There isn't one. There are three separate models, and the audio ones are Azure
Speech, not a realtime model. From `scripts/probe_agent_session.py`:

| Stage | Component | Value |
|---|---|---|
| **Ears** | speech to text | `azure-speech` (Azure Speech STT, 24 kHz) |
| **Brain** | LLM | `gpt-5` — from the agent version, **not reported in the session** |
| **Mouth** | text to speech | `en-US-AvaMultilingualNeural`, type `azure-standard` |
| — | turn detection | `azure_semantic_vad_multilingual` |
| — | noise / echo | `azure_deep_noise_suppression`, `server_echo_cancellation` |

So "the voice model" is an **Azure Neural TTS voice**. It is not GPT Realtime, and no
realtime or audio model appears anywhere in the session.

The single most telling field is `session.model`:

```json
{
  "id": "sess_42ZBmG4US2vQdz0y9xf9CA",
  "model": "rfp-voice-agent",          // <- the AGENT occupies the model slot
  "voice": {"name": "en-US-AvaMultilingualNeural", "type": "azure-standard"},
  "input_audio_transcription": {"model": "azure-speech"},
  "agent": {"type": "agent", "name": "rfp-voice-agent"}
}
```

In direct-model mode that field holds a model name. In agent mode it holds the
*agent name* — the slot is already taken, which is precisely why passing `?model=`
has nothing to override.

### Which of those can you change?

| Component | Changeable? | How |
|---|---|---|
| TTS voice | yes | agent metadata, or `session.update` at runtime |
| Voice style / rate / temperature | yes | same `voice` object |
| STT model | yes | `azure-speech` or `mai-transcribe` |
| Turn detection, noise, echo | yes | same metadata block |
| **LLM** | only by creating a new agent version | `PromptAgentDefinition(model=...)` |
| **Swapping in a realtime model** | **no** | not available in agent mode at all |

---


### Region: francecentral is better than documented

The docs list HD voices for southeastasia, centralindia, swedencentral, westeurope,
eastus, eastus2 and westus2 only. francecentral is absent — but every voice tested
was accepted and echoed back unchanged:

| Voice | Result |
|---|---|
| `en-US-AvaMultilingualNeural` | PASS |
| `en-US-AvaNeural` | PASS |
| `en-US-Ava:DragonHDLatestNeural` | PASS (despite the region list) |
| `en-US-Harper:MAI-Voice-2-Flash` | PASS |
| `ava` (`azure-realtime-native`) | FAIL — needs the `azure-realtime` model |

Models reachable from this resource: `gpt-realtime-1.5`, `gpt-realtime`,
`gpt-realtime-mini`, `gpt-4o-mini`, `gpt-5`, `azure-realtime` all connected.
`phi4-mm-realtime` did not.

The default here is still `en-US-AvaMultilingualNeural`, since HD support in a region
Microsoft does not document is not something to depend on in production.

---

## What this means for the RFP agent

The two capabilities pull in opposite directions, so this repo ships both.

| | Track A — agent mode | Track B — direct model + BYOM |
|---|---|---|
| Brain | agent's chat deployment (`gpt-5`) | **your `gpt-realtime-1.5`** |
| Audio | cascaded STT → LLM → TTS | native speech-to-speech |
| RFP grounding | File Search, run by Foundry | `search_rfp`, run by your backend |
| Microsoft docs | MCP tool, run by Foundry | `search_docs`, run by your backend |
| Conversation history | Foundry threads + tracing | your problem |
| Model choice | fixed by agent version | yours |
| Who holds the socket | the client | your backend |

The distinction that matters in Track B is *who executes the tools*, not client
versus server. There is no Foundry agent to run File Search or MCP, so the process
holding the Voice Live socket has to do it — and that process is the **backend**
(`backend/bridge.py`), never the browser. The browser streams microphone audio and
plays audio back; it has no Azure credential and never sees the RFP corpus.

**Choose Track A** when you want Foundry to own orchestration — tools, threads,
tracing, versioning — and you can accept cascaded latency.

**Choose Track B** when latency or model/data-residency control dominates. Annex D
of the sample tender asks for a turn latency under 1.2 s at the 95th percentile,
which is exactly the kind of target that pushes you here.

There is no third option that gives you both today.

---

## Reproducing

```bash
az login
python scripts/probe_voicelive_region.py     # region + credentials gate
python scripts/probe_voice_matrix.py         # voices and models this region accepts
python scripts/probe_model_control.py        # the eight experiments above
python scripts/probe_agent_session.py        # the agent-mode pipeline, field by field
python scripts/test_backend_turn.py          # a full BYOM turn, no microphone needed
```
