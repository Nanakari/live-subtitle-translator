Live Subtitle Translator - Windows x64 portable edition

1. Extract the entire ZIP to a writable folder, such as your Downloads folder.
   Keep LiveSubtitleTranslator.exe and the _internal folder together.
2. Copy config.local.example.yaml to config.local.yaml beside the executable.
3. Edit config.local.yaml and put your Gemini API key in gemini.api_key.
   Alternatively, set the GEMINI_API_KEY environment variable before launching.
4. Double-click LiveSubtitleTranslator.exe. Python is included.
5. Play audio. Right-click the subtitles for options. Ctrl+Alt+Space pauses.
   Exit through the system tray or close the subtitle window.

Requirements: Windows 10/11 x64, a stereo playback device, Internet access,
and a Gemini API key with access to the configured Live model. API usage may
incur charges. Selected system playback audio is sent to Gemini for translation.

Settings and app.log are written beside the executable. Keep config.local.yaml
private. The release does not include a key, proxy, personal settings, or logs.
The executable is unsigned; Windows may display an unknown-publisher prompt.

Upgrade: extract the new version to a new folder, then copy your private
config.local.yaml and optional desktop_state.json into that folder.

This build passes offline dependency/configuration/Tk smoke tests. Real Gemini
translation and audio-device behavior still depend on your setup.

Source: https://github.com/Nanakari/live-subtitle-translator
