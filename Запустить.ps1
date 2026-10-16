param(
    [int]$Speakers = 0,
    [int]$MinSpeakers = 0,
    [int]$MaxSpeakers = 0,
    [switch]$Watch,
    [switch]$Force,
    [switch]$RenderOnly,
    [switch]$Check
)

& "$PSScriptRoot\pipeline\run.ps1" @PSBoundParameters
exit $LASTEXITCODE
