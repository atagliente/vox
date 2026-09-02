"""The demonstration certificate authority shipped with VOX.

Its private key is in this package, and therefore in every copy of VOX and in
the public repository. That is deliberate: a fresh download can join a mesh
without provisioning anything. It also means anyone holding VOX can issue an
identity for this authority, so it is a demonstration, not a security boundary.

``/mesh new-ca`` replaces it with an authority private to your machines.
"""
