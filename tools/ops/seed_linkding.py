"""Trusted evaluator fixture, executed via stdin inside the owned lab container.

Output contains local credentials. lab.py captures it without logging and saves
credentials only under ignored .local/ops-lab. Never send this output to a model.
"""
import hashlib
import importlib
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import User
from django.utils import timezone

from bookmarks.models import ApiToken, Bookmark, BookmarkAsset

username = "aura_lab"
user, _ = User.objects.get_or_create(username=username)
user.set_unusable_password()
user.save()
profile = user.profile
profile.enable_automatic_html_snapshots = False
profile.enable_favicons = False
profile.enable_preview_images = False
profile.web_archive_integration = "disabled"
profile.save()
bookmark, _ = Bookmark.objects.get_or_create(
    owner=user, url="https://example.invalid/aura-local-fixture",
    defaults={"title": "Local evaluator asset", "date_added": timezone.now(),
              "date_modified": timezone.now()},
)
asset_dir = Path(settings.LD_ASSET_FOLDER)
asset_dir.mkdir(parents=True, exist_ok=True)
asset_file = asset_dir / "aura-payload.bin"
size = 500 * 1024 * 1024
# No newlines: the historical API line iterator must exercise the actual bug.
chunk = b"A" * (1024 * 1024)
digest = hashlib.sha256()
with asset_file.open("wb") as output:
    for _ in range(500):
        output.write(chunk)
        digest.update(chunk)
asset_file.chmod(0o644)
asset, _ = BookmarkAsset.objects.update_or_create(
    bookmark=bookmark, file=asset_file.name,
    defaults={"file_size": size, "asset_type": "upload", "content_type": "application/octet-stream",
              "display_name": "aura-payload.bin", "status": "complete", "gzip": False},
)
token, _ = ApiToken.objects.get_or_create(user=user, name="AURA local lab")
session = importlib.import_module(settings.SESSION_ENGINE).SessionStore()
session[SESSION_KEY] = str(user.pk)
session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
session[HASH_SESSION_KEY] = user.get_session_auth_hash()
session.set_expiry(86400)
session.save()
print("AURA_SEED=" + json.dumps({
    "token": token.key, "sessionid": session.session_key,
    "session_cookie_name": settings.SESSION_COOKIE_NAME,
    "bookmark_id": bookmark.pk, "asset_id": asset.pk,
    "expected_bytes": size, "expected_sha256": digest.hexdigest(),
}))
