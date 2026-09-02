# Demo quickstart

The run instructions now live in one place, so they cannot drift apart:

👉 **[README — Run the demos](../README.md#run-the-demos)**

That section covers prerequisites and Azure resources, bootstrap, one-time provisioning,
all three scenarios, the verification probes, and troubleshooting.

Shortest possible path:

```powershell
.\scripts\setup_demo.ps1                                  # venv + deps + .env
az login                                                  # then fill in .env
.\.venv\Scripts\python.exe agent\setup_knowledge.py       # writes VECTOR_STORE_ID to .env
.\scripts\run_demo.ps1 -Scenario agent -Provision         # create the agent, then talk
```
