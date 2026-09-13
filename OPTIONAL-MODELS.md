# Optional MusicGen integration

The source includes an AudioCraft adapter. It is disabled in the demonstration configuration and excluded from the default requirements because its legacy Torch constraints conflict with modern media stacks. Install it only in a separately resolved environment after checking the provider requirements. Core clipping does not require AudioCraft.

## Face detection

The original face-tracking adapter uses the legacy MediaPipe Solutions API. It is optional and excluded from the default install because it previously forced an old protobuf version. The default is center cropping; face modes can fall back to center if the adapter is unavailable. Modernizing that optional adapter and verifying its dependency graph remain separate work.
