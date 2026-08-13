"""The decisive experiment: how much control do you have over the voice model?

Voice Live exposes a *brain* model three different ways, and they are not
interchangeable. This script runs each combination against the live service and
reports exactly what the API does, so the answer is evidence rather than inference.

Experiments
-----------
1. managed-realtime   ``?model=gpt-realtime-1.5``
                      Fully managed. Voice Live hosts the model; your own deployment
                      is not involved and you are not billed for it.

2. byom-realtime      ``?profile=byom-azure-openai-realtime&model=<deployment>``
                      Routes to *your* deployment - your data zone, your content
                      filter, your quota.

3. agent-mode         ``?agent_name=...&project_name=...``
                      The brain is whatever chat deployment the agent version was
                      created with. Note there is no ``model`` parameter here.

4. agent+model        agent params AND ``model`` together.

5. agent+byom         agent params AND ``profile=byom-...`` together.
                      If this works, you can point an agent at a realtime deployment.
                      If it does not, agent mode and BYOM are mutually exclusive.

Experiments 3-5 are skipped unless --agent-name is supplied or AGENT_NAME resolves
to an agent that already exists.

Usage:
    python scripts/probe_model_control.py
    python scripts/probe_model_control.py --agent-name rfp-voice-agent
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import Modality, RequestSession, ServerEventType
from azure.identity.aio import AzureCliCredential

from agent._common import Settings, dumps

EVENT_TIMEOUT_SECONDS = 30


@dataclass
class Experiment:
    name: str
    question: str
    connect_kwargs: dict[str, Any]
    #: Optional voice to request, used by the native-voice control.
    voice: dict[str, str] | None = None


@dataclass
class Outcome:
    ok: bool
    detail: str
    session_summary: str = ""


async def run_experiment(
    settings: Settings, credential: AzureCliCredential, exp: Experiment
) -> Outcome:
    try:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            **exp.connect_kwargs,
        ) as connection:
            # An agent supplies its own instructions; sending them is rejected.
            is_agent = "agent_name" in exp.connect_kwargs
            session_kwargs: dict[str, Any] = {"modalities": [Modality.TEXT, Modality.AUDIO]}
            if not is_agent:
                session_kwargs["instructions"] = "Probe session. Do not speak."
            if exp.voice is not None:
                session_kwargs["voice"] = exp.voice
            await connection.session.update(session=RequestSession(**session_kwargs))

            async def read() -> Outcome:
                async for event in connection:
                    if event.type == ServerEventType.SESSION_UPDATED:
                        session = event.session
                        agent = getattr(session, "agent", None)
                        summary = f"session={session.id}"
                        if agent is not None:
                            summary += (
                                f" agent.name={getattr(agent, 'name', None)}"
                                f" agent.id={getattr(agent, 'agent_id', None)}"
                            )
                        voice = session.voice
                        if isinstance(voice, dict):
                            summary += f" voice={voice.get('name')}"
                        return Outcome(True, "session established", summary)
                    if event.type == ServerEventType.ERROR:
                        return Outcome(False, f"service error: {event.error.message[:160]}")
                return Outcome(False, "stream ended with no session.updated")

            return await asyncio.wait_for(read(), EVENT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return Outcome(False, f"timeout after {EVENT_TIMEOUT_SECONDS}s")
    except Exception as exc:  # noqa: BLE001 - diagnostic tool, report everything
        return Outcome(False, f"{type(exc).__name__}: {str(exc)[:200]}")


def build_experiments(settings: Settings, agent_name: str | None) -> list[Experiment]:
    api = settings.api_version
    agent_api = settings.agent_api_version
    realtime = settings.realtime_deployment_name

    experiments = [
        Experiment(
            "1. managed-realtime",
            "Can Voice Live run gpt-realtime-1.5 fully managed?",
            {"api_version": api, "model": realtime},
        ),
        Experiment(
            "2. byom-realtime",
            "Can Voice Live route to MY gpt-realtime-1.5 deployment?",
            {
                "api_version": api,
                "model": realtime,
                "query": {"profile": settings.byom_mode},
            },
        ),
    ]

    if agent_name:
        agent_params = {
            "api_version": agent_api,
            "agent_name": agent_name,
            "project_name": settings.project_name,
        }
        experiments += [
            Experiment(
                "3. agent-mode",
                "Does plain agent mode connect, and what does it report?",
                dict(agent_params),
            ),
            Experiment(
                "4. agent+model",
                "Can I override the agent's brain with ?model=gpt-realtime-1.5?",
                {**agent_params, "model": realtime},
            ),
            Experiment(
                "5. agent+byom",
                "Can an agent be backed by MY realtime deployment via BYOM?",
                {**agent_params, "model": realtime, "query": {"profile": settings.byom_mode}},
            ),
            # Controls. A connection succeeding is a weak signal - the service may be
            # accepting the parameter and discarding it. If a deliberately invalid
            # value ALSO connects, then experiments 4 and 5 proved nothing except
            # that the parameter is ignored in agent mode.
            Experiment(
                "6. agent+bogus-model (control)",
                "Does a nonsense model name also connect? If yes, ?model= is ignored.",
                {**agent_params, "model": "this-model-does-not-exist-xyz"},
            ),
            Experiment(
                "7. agent+bogus-profile (control)",
                "Does a nonsense BYOM profile also connect? If yes, ?profile= is ignored.",
                {**agent_params, "query": {"profile": "byom-not-a-real-profile-xyz"}},
            ),
            # If agent mode really were running a native speech-to-speech model, a
            # native voice would be accepted. Under the cascaded Azure TTS path it
            # is rejected the same way it was for gpt-4o-mini.
            Experiment(
                "8. agent+native-voice (control)",
                "Will agent mode accept an azure-realtime-native voice?",
                dict(agent_params),
                voice={"name": "ava", "type": "azure-realtime-native"},
            ),
        ]

    return experiments


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=None)
    args = parser.parse_args()

    settings = Settings.load()
    settings.require("VOICELIVE_ENDPOINT", "PROJECT_NAME")

    agent_name = args.agent_name or settings.agent_name or None

    print(f"Endpoint            : {settings.voicelive_endpoint}")
    print(f"Project             : {settings.project_name}")
    print(f"Realtime deployment : {settings.realtime_deployment_name}")
    print(f"BYOM profile        : {settings.byom_mode}")
    print(f"Agent               : {agent_name or '(none - skipping agent experiments)'}")
    print("=" * 78)

    results: list[tuple[Experiment, Outcome]] = []
    async with AzureCliCredential() as credential:
        for exp in build_experiments(settings, agent_name):
            print(f"\n{exp.name}  -  {exp.question}")
            printable = {k: v for k, v in exp.connect_kwargs.items() if k != "api_version"}
            print(f"  params: {dumps(printable)}")
            outcome = await run_experiment(settings, credential, exp)
            print(f"  {'PASS' if outcome.ok else 'FAIL'}: {outcome.detail}")
            if outcome.session_summary:
                print(f"  {outcome.session_summary}")
            results.append((exp, outcome))

    print("\n" + "=" * 78)
    print("SUMMARY")
    outcomes = {exp.name: outcome for exp, outcome in results}
    for exp, outcome in results:
        print(f"  {'PASS' if outcome.ok else 'FAIL'}  {exp.name}")

    print("\nVERDICT")
    bogus_model = outcomes.get("6. agent+bogus-model (control)")
    bogus_profile = outcomes.get("7. agent+bogus-profile (control)")
    native_voice = outcomes.get("8. agent+native-voice (control)")

    if bogus_model is None:
        print("  Agent experiments were skipped; pass --agent-name to run them.")
        return 0

    # "Connected" is not the same as "honoured". If an obviously invalid value is
    # accepted too, then the parameter is being discarded rather than applied.
    if bogus_model.ok:
        print("  ?model=   is IGNORED in agent mode (a nonsense model name connects fine).")
        print("            => experiment 4 is a false positive.")
    else:
        print("  ?model=   is validated in agent mode.")

    if bogus_profile and bogus_profile.ok:
        print("  ?profile= is IGNORED in agent mode (a nonsense profile connects fine).")
        print("            => experiment 5 is a false positive; BYOM does not apply to agents.")
    else:
        print("  ?profile= is validated in agent mode.")

    if native_voice and not native_voice.ok:
        print("  Agent mode rejects azure-realtime-native voices => audio is CASCADED")
        print("            (Azure STT -> agent's chat model -> Azure TTS), not speech-to-speech.")

    print(
        "\n  Bottom line: in agent mode the brain is fixed by the agent version's\n"
        "  model deployment. To run YOUR gpt-realtime-1.5 deployment you must use\n"
        "  direct-model mode with profile=byom-azure-openai-realtime (experiment 2),\n"
        "  which means no server-side agent, so RAG and MCP move to the client."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
