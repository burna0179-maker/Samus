<#
.SYNOPSIS
  Provision the HustleForge callback inbound squad:
    1. Warm Morgan assistant  — handles warm-transfer from Receptionist
    2. Receptionist assistant — answers inbound, looks up caller, transfers
    3. Sales squad update     — adds both to f39b6ec6 Sales squad

.DESCRIPTION
  Run once to create the assistants. The Receptionist is pointed at the
  AWS API Gateway callback-lookup tool so it can surface prospect context
  on every inbound call. After running, bind all 6 Vapi marketing numbers
  to the Receptionist as their inbound assistant in the Vapi dashboard.

.EXAMPLE
  .\Provision-CallbackSquad.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
Import-Module $sharedModule -Force

$vapiKey = Get-HfSecret -Scope Samus -Name VapiApiKey
$headers = @{
    'Authorization' = "Bearer $vapiKey"
    'Content-Type'  = 'application/json'
}

$CALLBACK_LOOKUP_URL = 'https://zbp9q9pzdl.execute-api.us-west-1.amazonaws.com/callback-lookup'
$SALES_SQUAD_ID      = 'f39b6ec6-40c4-4244-946c-5bf7a7fdaadb'

# ─────────────────────────────────────────────────────────────────────────────
# 1. Warm Morgan
# ─────────────────────────────────────────────────────────────────────────────
$warmMorganBody = @{
    name = 'Warm Morgan (Callback)'
    model = @{
        provider = 'anthropic'
        model    = 'claude-haiku-4-5'
        messages = @(
            @{
                role    = 'system'
                content = @"
You are Morgan, a friendly sales rep at HustleForge. You're speaking with someone who called back after a previous conversation with our team. This is a WARM callback — treat them with familiarity, not a cold pitch.

CONTEXT VARIABLES (injected by the Receptionist on transfer):
- {{prospect_company}}: the caller's company name
- {{prospect_owner}}: the owner's name
- {{offer_summary}}: the offer discussed on the previous call
- {{last_intent_score}}: how interested they seemed (0-100)

OPENING: Reference that they're calling back. Example: "Hey, thanks for calling us back — is this {{prospect_owner}} from {{prospect_company}}?"

IF prospect is KNOWN (variables filled):
- You already know what was discussed: {{offer_summary}}
- Your goal is to move them to a booked call or next commitment
- Be warm, consultative, not a re-pitch

IF prospect is UNKNOWN (variables empty):
- Greet them warmly, ask their name and company
- Briefly re-qualify interest in HustleForge's AI workflow automation services
- If interested, book a discovery call

ALWAYS:
- Keep it conversational, not scripted
- If they want to schedule a call, offer Monday–Friday 9am–5pm Pacific
- If not ready, take their email for follow-up
- Keep calls under 5 minutes

END OF CALL — emit this JSON in structuredData:
{
  "callback_summary": {
    "company": "<company name>",
    "owner_name": "<contact name>",
    "outcome": "booked_call|follow_up_email|not_interested|wrong_number",
    "booked_time": "<ISO datetime or empty>",
    "follow_up_email": "<email or empty>",
    "notes": "<1-2 sentences>"
  }
}
"@
            }
        )
        temperature = 0.6
    }
    voice = @{
        provider    = '11labs'
        voiceId     = 'cgSgspJ2msm6clMCkdW9'
        speed       = 0.90
        similarityBoost = 0.75
    }
    firstMessage = 'Hey, thanks for calling HustleForge back. This is Morgan -- who am I speaking with?'
    endCallMessage = 'Great talking with you. We will be in touch soon -- have a great day!'
    transcriber = @{
        provider = 'deepgram'
        model    = 'nova-2'
        language = 'en-US'
    }
    backgroundDenoisingEnabled = $true
    structuredDataEnabled       = $true
} | ConvertTo-Json -Depth 10

Write-Host "Creating Warm Morgan assistant..." -ForegroundColor Cyan
$warmMorganResp = Invoke-RestMethod -Uri 'https://api.vapi.ai/assistant' `
    -Method POST -Headers $headers -Body $warmMorganBody
$warmMorganId = $warmMorganResp.id
Write-Host "  Created: $warmMorganId" -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────────────────
# 2. Receptionist
# ─────────────────────────────────────────────────────────────────────────────
$receptionistBody = @{
    name = 'HustleForge Receptionist (Inbound)'
    model = @{
        provider = 'anthropic'
        model    = 'claude-haiku-4-5'
        messages = @(
            @{
                role    = 'system'
                content = @"
You are a friendly receptionist at HustleForge. Your ONLY job is to:
1. Answer the call warmly
2. Use the callback_lookup tool to check if this caller is a known prospect
3. If known: greet them by name, briefly acknowledge the previous conversation, then transfer to Warm Morgan with their context
4. If unknown: get their name and company, then transfer to Warm Morgan

OPENING: "Thank you for calling HustleForge! This is the front desk — let me pull up your information."
[immediately call callback_lookup tool with {{customer.number}}]

AFTER LOOKUP:
- If found=true: "Hi {{owner_name}}, great to hear from you! I'll connect you with Morgan who can pick up right where you left off." → transfer
- If found=false: "Thanks for calling! Can I get your name and company so I can connect you with the right person?" → get info → transfer

TRANSFER: Always transfer to Warm Morgan. Pass context in the transfer message.

Keep your part under 30 seconds. You are a routing layer, not a conversationalist.
"@
            }
        )
        temperature = 0.3
        tools = @(
            @{
                type = 'function'
                function = @{
                    name        = 'callback_lookup'
                    description = 'Look up whether an inbound caller is a known HustleForge prospect by their phone number. Always call this immediately when a call starts.'
                    parameters  = @{
                        type       = 'object'
                        properties = @{
                            phone = @{
                                type        = 'string'
                                description = 'The caller E.164 phone number, e.g. +15005550006'
                            }
                        }
                        required = @('phone')
                    }
                }
                server = @{
                    url = $CALLBACK_LOOKUP_URL
                }
            }
        )
    }
    voice = @{
        provider    = '11labs'
        voiceId     = 'cgSgspJ2msm6clMCkdW9'
        speed       = 0.95
        similarityBoost = 0.70
    }
    firstMessage = 'Thank you for calling HustleForge! One moment while I pull up your information.'
    transcriber = @{
        provider = 'deepgram'
        model    = 'nova-2'
        language = 'en-US'
    }
    backgroundDenoisingEnabled = $true
} | ConvertTo-Json -Depth 10

Write-Host "Creating Receptionist assistant..." -ForegroundColor Cyan
$receptionistResp = Invoke-RestMethod -Uri 'https://api.vapi.ai/assistant' `
    -Method POST -Headers $headers -Body $receptionistBody
$receptionistId = $receptionistResp.id
Write-Host "  Created: $receptionistId" -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────────────────
# 3. Update Sales Squad — add Receptionist + Warm Morgan as members
# ─────────────────────────────────────────────────────────────────────────────
# First fetch current squad members so we don't clobber existing members
$squad = Invoke-RestMethod -Uri "https://api.vapi.ai/squad/$SALES_SQUAD_ID" `
    -Method GET -Headers $headers

$existingMembers = @()
if ($squad.members) {
    $existingMembers = $squad.members | ForEach-Object {
        @{ assistantId = $_.assistantId; assistantDestinations = $_.assistantDestinations }
    }
}

# Add Receptionist (entry point) and Warm Morgan (transfer target)
$newMembers = $existingMembers + @(
    @{
        assistantId = $receptionistId
        assistantDestinations = @(
            @{
                assistantName = 'Warm Morgan (Callback)'
                message       = 'Transferring you to Morgan now -- she has your full context.'
                description   = 'Transfer to Warm Morgan with prospect context after lookup'
            }
        )
    },
    @{
        assistantId = $warmMorganId
        assistantDestinations = @()
    }
)

$squadPatch = @{ members = $newMembers } | ConvertTo-Json -Depth 10
Write-Host "Updating Sales squad $SALES_SQUAD_ID..." -ForegroundColor Cyan
Invoke-RestMethod -Uri "https://api.vapi.ai/squad/$SALES_SQUAD_ID" `
    -Method PATCH -Headers $headers -Body $squadPatch | Out-Null
Write-Host "  Squad updated." -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Callback Squad Provisioned ===" -ForegroundColor Green
Write-Host "Warm Morgan ID  : $warmMorganId"
Write-Host "Receptionist ID : $receptionistId"
Write-Host ""
Write-Host "NEXT STEP: In the Vapi dashboard, set the Receptionist ($receptionistId)"
Write-Host "as the inbound assistant for all 6 marketing phone numbers."
Write-Host ""
Write-Host "Marketing numbers to update:"
Write-Host "  a0d742b4-250e-4e36-9797-fdf650628790"
Write-Host "  21f79fb4-12f7-46e2-baa0-c07406fbb0f6"
Write-Host "  b93fbb87-02a9-4178-b3bd-3a38f6de6a0d"
Write-Host "  8554830e-1220-40c7-8cea-a48c41ee6d1e"
Write-Host "  4e944525-1629-4ee4-809b-64a99c0e66fc"
Write-Host "  33842c67-fef6-406c-959b-61891485a0b6"
