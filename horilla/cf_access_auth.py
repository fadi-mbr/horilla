"""
Cloudflare Access SSO for MBR Horilla.

people.mbrme.com sits behind Cloudflare Access (Google IdP, @mbrme.com policy).
CF Access authenticates the user at the edge and injects the header
`Cf-Access-Authenticated-User-Email`. This module trusts that header and
auto-logs-in the matching HorillaUser (by email) — so users sign in with
Google once (at CF Access) and land in Horilla already authenticated.

For the match to work, a HorillaUser's `email` must equal the user's Google
address (e.g. admin@mbrme.com, basel@mbrme.com). Unmatched identities fall
through to the normal username/password form.

SECURITY: this trusts an HTTP header, which is only safe because the app's
origin port is bound to 127.0.0.1 on the Coolify-B VM and is reachable ONLY
via the cloudflared tunnel (i.e. only *after* CF Access). Keep the port private.

`/api/*` is CF-Access-bypassed and uses JWT auth, so it never carries this
header and is unaffected (RemoteUser stays a no-op there).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import RemoteUserBackend
from django.contrib.auth.middleware import PersistentRemoteUserMiddleware
from django.shortcuts import redirect


class CloudflareAccessMiddleware(PersistentRemoteUserMiddleware):
    """Auto-login from the CF Access email header; bounce authed users off /login/."""

    header = "HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL"

    def process_request(self, request):
        # The base RemoteUser middleware decides "is this user already logged in?"
        # by comparing request.user.get_username() to the header value. Our header
        # carries an EMAIL while the matched user's USERNAME is different (e.g.
        # "admin"), so that check never matches and the base class calls
        # auth.login() on *every* request. Each login() rotates the CSRF token,
        # which silently invalidates the token embedded in already-rendered forms
        # — so every POST fails with 403 (GETs are unaffected, hence browsing
        # works but no form ever saves). Short-circuit when the session already
        # holds the user whose email matches the header: no re-login, no rotation.
        email = request.META.get(self.header)
        if email and request.user.is_authenticated:
            current = (getattr(request.user, "email", "") or "").strip().lower()
            if current == email.strip().lower():
                return
        return super().process_request(request)

    def __call__(self, request):
        # The inherited RemoteUser process_request auto-logs-in from the header.
        response = super().__call__(request)
        # Horilla's /login/ renders the form even for authenticated users, so
        # after CF Access auto-login send them straight to the dashboard.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and request.path.rstrip("/") == "/login":
            return redirect("/")
        return response


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
