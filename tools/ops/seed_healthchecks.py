"""Trusted holdout fixture; credential output is captured privately by its CLI."""
import importlib
import json
import secrets

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import User
from django.urls import reverse
from hc.accounts.models import Profile, Project

user, _ = User.objects.get_or_create(username="aura_holdout", defaults={"email": "aura@example.invalid"})
password = secrets.token_urlsafe(24)
user.set_password(password)
user.save()
profile = Profile.objects.for_user(user)
profile.reports = "off"
profile.save()
project, _ = Project.objects.get_or_create(owner=user, name="AURA portability holdout",
    defaults={"badge_key": secrets.token_hex(20)})
session = importlib.import_module(settings.SESSION_ENGINE).SessionStore()
session[SESSION_KEY] = str(user.pk)
session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
session[HASH_SESSION_KEY] = user.get_session_auth_hash()
session.set_expiry(86400)
session.save()
print("AURA_HEALTHCHECKS=" + json.dumps({
    "email": user.email, "password": password,
    "cookie": settings.SESSION_COOKIE_NAME + "=" + session.session_key,
    "checks_path": reverse("hc-checks", args=[project.code]),
}))
