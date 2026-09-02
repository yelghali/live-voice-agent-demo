[CmdletBinding()]
param(
    [ValidateSet('agent', 'byom', 'realtime')]
    [string]$Scenario = 'agent',

    # Agent scenario only: (re)build the vector store and publish a new agent version
    # before connecting. Provisioning is a one-time step, so it is opt-in.
    [switch]$Provision
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw 'Project environment not found. Run .\scripts\setup_demo.ps1 first.'
}

Push-Location $repoRoot
try {
    switch ($Scenario) {
        'agent' {
            if ($Provision) {
                # setup_knowledge.py writes VECTOR_STORE_ID back to .env, so
                # create_rfp_agent.py picks it up and attaches File Search.
                & $python agent\setup_knowledge.py
                if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
                & $python agent\create_rfp_agent.py
                if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            }
            & $python agent\voice_live_agent_client.py
        }
        'byom' {
            & $python -m backend.server
        }
        'realtime' {
            & $python scripts\probe_aoai_realtime_rag.py
        }
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}