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

**The speech legs** are Azure Speech, and the guarantee is explicit and covers
processing, not just storage:

    "Azure Speech doesn't store or process your data outside the region of your
     Azure Speech resource. The data is stored or processed only in the region
     where the resource is created."
    -- https://learn.microsoft.com/azure/ai-services/speech-service/regions

So STT and TTS stay in your resource's region, full stop. The only thing to check is
whether the *feature* you want exists there at all - a voice family that is not
offered in your region is a capability question, not a residency one.

**A trap on the brain leg**: Voice Live's own pre-deployed models have a deployment
type that varies *per region*, and it is not always Data Zone. In `francecentral`,
Voice Live serves managed `gpt-realtime-1.5` as **Global standard**, while the same
model deployed by you as `DataZoneStandard` stays in the EU. Using BYOM is therefore
a residency decision, not only a control one.

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

# Regions offering each voice family, from the Text to speech tab of the Azure Speech
# regions page. This is an availability question - wherever a voice runs, it runs in
# your resource's region.
VOICE_FAMILY_REGIONS: dict[str, set[str]] = {
    "DragonHD": {
        "canadacentral", "centralindia", "eastus", "eastus2", "francecentral",
        "southeastasia", "swedencentral", "westeurope", "westus2",
    },
    "MAI-Voice": {
        "canadacentral", "centralindia", "eastus", "eastus2", "francecentral",
        "southeastasia", "swedencentral", "westeurope", "westus2",
    },
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
    if "DragonHD" in voice_name:
        return "DragonHD"
    if "MAI-Voice" in voice_name:
        return "MAI-Voice"
    return None  # standard neural: broadly available


def check_voice(voice_name: str, region: str) -> tuple[bool, str]:
    """Is this voice family offered in this region? (Availability, not residency.)"""
    family = voice_family(voice_name)
    if family is None:
        return True, "standard neural voice - available in dozens of regions"

    regions = VOICE_FAMILY_REGIONS.get(family, set())
    if region in regions:
        return True, f"{family} voices are offered in {region}"
    return False, (
        f"{family} voices are NOT offered in {region} "
        f"(offered in: {', '.join(sorted(regions))})"
    )


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
    print("  Azure Speech does not store or process data outside the resource region,")
    print(f"  so STT and TTS both stay in {region}.")
    voice_ok, voice_note = check_voice(settings.voice_name, region)
    print(f"  Configured voice: {settings.voice_name}")
    print(f"    {'OK ' if voice_ok else 'WARN'} - {voice_note}")
    if not voice_ok:
        print("    This is an availability gap, not a residency risk.")

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
