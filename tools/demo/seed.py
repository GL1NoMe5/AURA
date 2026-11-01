"""Run only inside the new judging fixture; stdout is captured, never published."""
import os
from pathlib import Path
from django.contrib.auth.models import User
from django.utils import timezone
from bookmarks.models import ApiToken, Bookmark

user = User.objects.create(username="aura_demo")
user.set_unusable_password()
user.save()
profile = user.profile
profile.enable_automatic_html_snapshots = False
profile.enable_favicons = False
profile.enable_preview_images = False
profile.web_archive_integration = "disabled"
profile.save()
Bookmark.objects.create(owner=user, url="https://example.invalid/aura",
                        title="AURA test bookmark", date_added=timezone.now(), date_modified=timezone.now())
token = ApiToken.objects.create(user=user, name="demo-probe")
# The pinned uWSGI configuration drops to www-data (33:33). Initialization
# runs as root; hand over only this fresh, owned data volume before serving it.
data = Path('/etc/linkding/data')
for path in (data, *data.rglob('*')):
    if path.is_symlink():
        raise RuntimeError('Unexpected link in disposable data volume')
    os.chown(path, 33, 33)
print("AURA_TOKEN=" + token.key)
