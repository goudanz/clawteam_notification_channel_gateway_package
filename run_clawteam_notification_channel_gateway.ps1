param(
  [string]$ChannelsFile = ".\configs\channels.yaml",
  [string]$BindingsFile = ".\configs\bindings.yaml"
)

$env:CBG_CHANNELS = $ChannelsFile
$env:CBG_BINDINGS = $BindingsFile

python .\main.py
