# Samus operator scripts

## Secrets — local dev (Windows DPAPI)

Secrets used by the Samus local Compose stack live in a **DPAPI-encrypted
per-user store** under `%LOCALAPPDATA%\Samus\credentials\<name>.cred`. The
ciphertext is bound to the current Windows user on the current machine —
copying the file to another box or another user account decrypts to nothing.

This is the same shape as `C:\hardening_baseline\agent_credentials\svc_Samus.cred`
but in the user profile (no admin needed).

### One-time setup

```powershell
cd D:\Hustleforge\Samus\scripts
Import-Module .\Samus.Secrets.psm1
Set-SamusSecret -Name HivemindPassword
# (prompts: paste your Hivemind Neo4j password, press Enter)
```

After this, `Samus\docker\compose\.env` no longer needs `NEO4J_PASSWORD=...` —
the start script reads from the DPAPI store and exports it only for the lifetime
of the `docker compose` child process.

### Bring up the stack

```powershell
cd D:\Hustleforge\Samus\scripts
.\Start-SamusStack.ps1            # docker compose up -d
.\Start-SamusStack.ps1 -Rebuild   # force image rebuild
```

### Tear down

```powershell
.\Stop-SamusStack.ps1                  # compose down (preserves volumes)
.\Stop-SamusStack.ps1 -RemoveVolumes   # compose down -v
```

### Inspect / manage the local secret store

```powershell
Import-Module .\Samus.Secrets.psm1
Get-SamusSecretList                  # names of stored secrets
Test-SamusSecret -Name HivemindPassword
Remove-SamusSecret -Name HivemindPassword
```

## Secrets — Cloud Run (GCP Secret Manager)

The Cloud Build pipeline (`Samus/docker/cloudbuild.yaml`) binds Cloud Run env
vars to GCP Secret Manager secrets via `--set-secrets`. To rotate the Hivemind
password for the deployed `samus-memory-2026` (and any other workcell that
talks to Neo4j):

```powershell
# one-time create (skip if already exists)
gcloud secrets create hivemind-password --replication-policy=automatic `
    --project=${GCP_PROJECT_NUMBER}

# add a new version
"YOUR_REAL_PASSWORD" | gcloud secrets versions add hivemind-password `
    --data-file=- --project=${GCP_PROJECT_NUMBER}

# (cloudbuild.yaml already references hivemind-password:latest — next
#  `gcloud builds submit` deploy picks up the new version)
```

The local DPAPI store and the GCP Secret Manager secret are independent —
local-dev secrets never leave the workstation; Cloud Run secrets never touch
the workstation. Rotate each independently.
