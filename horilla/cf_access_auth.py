"""
Cloudflare Access SSO for MBR Horilla.

people.mbrme.com sits behind Cloudflare Access (Google IdP, @mbrme.com policy).
CF Access authenticates the user at the edge and injects the header
`Cf-Access-Authenticated-User-Email`. This module trusts that header and
auto-logs-in the matching HorillaUser — so users sign in with Google once
(at CF Access) and land in Horilla already authenticated. No second login.

SECURITY: this trusts an HTTP header, which is only safe because the app's
origin port is bound to 127.0.0.1 on the Coolify-B VM and is reachable ONLY
via the cloudflared tunnel (i.e. only *after* CF Access). If the origin were
ever exposed directly, the header could be spoofed. Keep the port private.

The `/api/*` path is CF-Access-bypassed and uses JWT auth, so it never carries
this header and is unaffected (RemoteUser stays a no-op there).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import RemoteUserBackend
from django.contrib.auth.middleware import PersistentRemoteUserMiddleware


class CloudflareAccessMiddleware(PersistentRemoteUserMiddleware):
    """Log the user in from the CF Access email header; persist across requests."""

    header = "HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL"


class CloudflareAccessBackend(RemoteUserBackend):
    """Match the CF Access email to an existing, active HorillaUser (no auto-provision)."""

    create_unknown_user = False

    def authenticate(self, request, remote_user):
        if not remote_user:
            return None
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=remote_user.strip(), is_active=True)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None
        return user if self.user_can_authenticate(user) else None
