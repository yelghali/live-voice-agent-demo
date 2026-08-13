"""Where is each leg of the voice pipeline actually processed?

Agent voice mode has three legs, and they have *different* residency stories:

  ears   Azure Speech STT
  brain  an LLM  (the agent's chat deployment)
  mouth  Azure Speech TTS

**The LLM leg** is governed by the deployment SKU, not by the resource region. Per the
Voice Live privacy doc, when you bring an agent or your own model, "data is processed
for model inferencing in accordance with the terms that apply to the relevant model":

  GlobalStandard / GlobalProvisionedManaged / GlobalBatch  -> ANY Azure region
  DataZoneStandard / DataZoneProvisionedManaged            -> within US / EU / APAC
  Standard / ProvisionedManaged                            -> the deployment region
  DeveloperTier                                            -> no residency guarantee

**The speech legs** are Azure Speech. Real-time speech to text is "processed only on
Azure's server memory" with "no data stored at rest", and Voice Live "does not store
or retain customer data". Opt-in debug logging (only when you raise a support ticket)
is stored "within the same resource region" and deleted after 30 days.

Note what that does and does not say. It is a strong statement about **retention**,
and a weaker one about **geography**: no public doc states that speech synthesis for a
given voice is performed in your resource's region. That matters when a voice is
accepted in a region the docs do not list it for - see the voice check below.

Usage:
    python scripts/probe_data_residency.py
    python scripts/probe_data_residency.py --require-zone EU
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from agent._common import Settings

# SKU -> (scope label, satisfies a data-zone requirement?)
SKU_SCOPE: dict[str, tuple[str, bool]] = {
    "GlobalStandard": ("any Azure region", False),
    "GlobalProvisionedManaged": ("any Azure region", False),
    "GlobalBatch": ("any Azure region", False),
    "DataZoneStandard": ("within the data zone", True),
    "DataZoneProvisionedManaged": ("within the data zone", True),
    "DataZoneBatch": ("within the data zone", True),
    "Standard": ("the deployment region", True),
    "ProvisionedManaged": ("the deployment region", True),
    "DeveloperTier": ("NO residency guarantee", False),
}

# Regions each voice family is *documented* for. A voice being accepted elsewhere is
# not evidence that it is synthesised there.
VOICE_FAMILY_REGIONS: dict[str, set[str]] = {
    "DragonHD": {
        "southeastasia", "centralindia", "swedencentral", "westeurope",
        "eastus", "eastus2", "westus2",
    },
    "DragonHDFlash": {"eastus", "westeurope", "southeastasia", "chinanorth3"},
    "MAI-Voice": set(),  # preview; no published region list
}


def list_deployments(resource: str, resource_group: str) -> list[dict]:
    """Read deployment SKUs via the Azure CLI (the SDK does not expose them)."""
    result = subprocess.run(
        [
            "az", "cognitiveservices", "account", "deployment", "list",
            "-n", resource, "-g", resource_group, "-o", "json",
        ],
        capture_output=True,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"az failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def resource_region(resource: str, resource_group: str) -> str:
    result = subprocess.run(
        [
            "az", "cognitiveservices", "account", "show",
            "-n", resource, "-g", resource_group,
            "--query", "location", "-o", "tsv",
        ],
        capture_output=True,
        text=True,
        shell=True,
    )
    return (result.stdout or "").strip() or "unknown"


def voice_family(voice_name: str) -> str | None:
    """Classify a voice name into a family with a published region list."""
    if "DragonHDFlash" in voice_name:
        return "DragonHDFlash"
    if "DragonHD" in voice_name:
        return "DragonHD"
    if "MAI-Voice" in voice_name:
        return "MAI-Voice"
    return None  # standard neural: broadly available


def check_voice(voice_name: str, region: str) -> tuple[bool, str]:
    """Is this voice documented for this region?"""
    family = voice_family(voice_name)
    if family is None:
        return True, "standard neural voice - available in dozens of regions"

    regions = VOICE_FAMILY_REGIONS.get(family, set())
    if not regions:
        return False, f"{family} has no published region list (preview)"
    if region in regions:
        return True, f"{family} is documented for {region}"
    return False, f"{family} is NOT documented for {region} (listed: {', '.join(sorted(regions))})"


def agent_model(settings: Settings, agent_name: str) -> str | None:
    with AzureCliCredential() as credential:
        project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)
        try:
            agent = project.agents.get(agent_name=agent_name)
        except Exception:  # noqa: BLE001
            return None
        latest = (agent.versions or {}).get("latest")
        definition = getattr(latest or agent, "definition", None)
        return getattr(definition, "model", None)


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", default="fdy-sa33b5nih2ogs")
    parser.add_argument("--resource-group", default="rg-grchatbot")
    parser.add_argument("--agent-name", default=settings.agent_name)
    parser.add_argument(
        "--require-zone",
        default="EU",
        help="Data zone the workload must stay in (label only, for the verdict).",
    )
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT")

    deployments = list_deployments(args.resource, args.resource_group)
    region = resource_region(args.resource, args.resource_group)
    print(f"Resource    : {args.resource}  ({region})")
    print(f"Requirement : processing must stay within {args.require_zone}\n")

    print("BRAIN - the LLM leg (governed by deployment SKU, not by region)")
    print(f"{'DEPLOYMENT':<26}{'SKU':<30}{'PROCESSED IN':<26}OK")
    print("-" * 90)
    compliant: dict[str, bool] = {}
    for dep in deployments:
        name = dep["name"]
        sku = (dep.get("sku") or {}).get("name", "?")
        scope, ok = SKU_SCOPE.get(sku, ("unknown", False))
        compliant[name] = ok
        print(f"{name:<26}{sku:<30}{scope:<26}{'yes' if ok else 'NO'}")

    print("\nEARS and MOUTH - the Azure Speech legs")
    print("-" * 90)
    print("  Real-time STT  : processed in server memory, nothing stored at rest")
    print("  Voice Live     : does not store or retain customer data")
    print("  Debug logging  : opt-in via support ticket only; stored in the SAME")
    print("                   resource region, deleted after 30 days")
    voice_ok, voice_note = check_voice(settings.voice_name, region)
    print(f"  Configured voice: {settings.voice_name}")
    print(f"    {'OK ' if voice_ok else 'WARN'} - {voice_note}")
    if not voice_ok:
        print("    A voice being accepted in a region is not evidence that it is")
        print("    synthesised there. Confirm with Microsoft before relying on it.")

    print("\n" + "=" * 90)
    model = agent_model(settings, args.agent_name)
    if model is None:
        print(f"Agent '{args.agent_name}' not found - skipping the agent verdict.")
        return 0

    ok = compliant.get(model)
    print(f"Agent '{args.agent_name}' brain: {model}")
    if ok is None:
        print(f"  Deployment '{model}' not found on this resource.")
        return 1
    if ok:
        print(f"  PASS - inference stays within {args.require_zone}.")
        return 0

    print(
        f"  FAIL - '{model}' is a Global deployment, so prompts and responses may be\n"
        f"         processed in ANY Azure region. Azure Speech still runs in the\n"
        f"         resource region, but the LLM leg breaks {args.require_zone} residency.\n"
        f"\n  Fix: create a new agent version on a Data Zone deployment, e.g.\n"
        f"       python agent/create_rfp_agent.py --model <data-zone-deployment>\n"
        f"       or deploy the same model as DataZoneStandard and point the agent at it."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
