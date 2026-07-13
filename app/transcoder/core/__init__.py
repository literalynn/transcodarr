"""Cœur métier : sonde ffprobe, matrice Dolby Vision, builder FFmpeg.

Tout ce module est constitué de fonctions pures ou de wrappers subprocess
fins : la logique (buckets, matrice DV, construction de commande, hash de
règle) est testable sans GPU ni binaire FFmpeg.
"""
