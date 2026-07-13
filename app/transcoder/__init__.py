"""Transcodeur automatique pour bibliothèques Jellyfin."""

# Version de l'outil, embarquée dans le tag conteneur TRANSCODER_TAG (§5.7)
# et dans media_files.processed_tool_version. Incrémenter la partie "v1"
# uniquement si un changement de comportement justifie un re-traitement global.
TOOL_NAME = "jftranscoder"
TOOL_VERSION = "v1"
TOOL_ID = f"{TOOL_NAME}-{TOOL_VERSION}"
