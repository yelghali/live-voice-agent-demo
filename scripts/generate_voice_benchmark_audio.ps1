[CmdletBinding()]
param(
    [string]$OutputPath = 'logs\foundry-voice-benchmark.wav',
    [string]$Text = 'Say hello briefly.'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedOutput = Join-Path $repoRoot $OutputPath
$outputDirectory = Split-Path -Parent $resolvedOutput

New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
Add-Type -AssemblyName System.Speech

$synthesizer = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        24000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $synthesizer.Rate = 0
    $synthesizer.SetOutputToWaveFile($resolvedOutput, $format)
    $synthesizer.Speak($Text)
}
finally {
    $synthesizer.Dispose()
}

$file = Get-Item $resolvedOutput
Write-Output "Created $($file.FullName) ($($file.Length) bytes)."
