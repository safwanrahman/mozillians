import re
from datetime import datetime

from django.conf import settings
from django.utils import translation


def i18n(request):
    return {'LANGUAGES': settings.LANGUAGES,
            'LANG': settings.LANGUAGE_URL_MAP.get(translation.get_language())
            or translation.get_language(),
            'DIR': 'rtl' if translation.get_language_bidi() else 'ltr',
            }


def globals(request):
    return {'request': request,
            'settings': settings}


def current_year(request):
    return {'current_year': datetime.today().year}


def canonical_path(request):
    """
    The canonical path can be overridden with a template variable like
    l10n_utils.render(request, template_name, {'canonical_path': '/firefox/'})
    """
    lang = getattr(request, 'locale', settings.LANGUAGE_CODE)
    url = getattr(request, 'path', '/')
    return {'canonical_path': re.sub(r'^/' + lang, '', url)}
