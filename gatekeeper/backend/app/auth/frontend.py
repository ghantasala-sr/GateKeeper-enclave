"""Hand a finished login back to the frontend.

A login started from the frontend (`/auth/.../login?next=app`) ends with a
redirect to FRONTEND_URL carrying the access token in the URL *fragment*
(`#access_token=...`). Browsers never send fragments to any server, so the
token doesn't land in server or proxy logs. The frontend reads it, clears it
from the address bar, and keeps it in memory only.

Logins started any other way (curl, tests) still get JSON back.
"""

from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import settings

NEXT_KEY = "login_next"


def remember_next(request: Request, next: str | None) -> None:
    if next == "app":
        request.session[NEXT_KEY] = "app"
    else:
        request.session.pop(NEXT_KEY, None)


def started_from_app(request: Request) -> bool:
    return request.session.pop(NEXT_KEY, None) == "app"


def redirect(**fragment) -> RedirectResponse:
    # 303 so a POST (the SAML ACS) becomes a GET of the frontend page.
    return RedirectResponse(f"{settings.frontend_url.rstrip('/')}/#{urlencode(fragment)}", status_code=303)


async def finish(request: Request, complete_login) -> dict | RedirectResponse:
    """Run the login's final step; send the result to the frontend if that's where it began."""
    from_app = started_from_app(request)
    try:
        result = await complete_login()
    except HTTPException as e:
        if from_app:
            return redirect(error=str(e.detail))
        raise
    if from_app:
        return redirect(access_token=result["access_token"], expires_in=result["expires_in"])
    return result
