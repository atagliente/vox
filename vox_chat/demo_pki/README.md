# The demo authority

`ca.crt` and `ca.key` here are a certificate authority that ships with VOX, so
two fresh installations on the same network segment can find each other without
anyone provisioning anything first.

**The private key is public.** It is in this repository and in every copy of
VOX, so anybody can issue themselves an identity for this authority. Treat a
demo mesh as open to whoever is on your network segment.

For anything else, replace it: `/mesh new-ca` inside VOX creates an authority
that exists only on your machine, reissues this agent against it, and moves the
demo files aside. Every machine that should join then needs a certificate from
*that* authority — see the mesh section of the README.
