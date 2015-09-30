import datetime
import itertools
import urllib
import urlparse

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.template import defaultfilters
from django.utils.encoding import smart_str
from django.utils.html import strip_tags

from jingo import register
import jinja2

from mozillians.settings.urlresolvers import reverse


def absolutify(url):
    """Takes a URL and prepends the SITE_URL"""
    site_url = getattr(settings, 'SITE_URL', False)

    # If we don't define it explicitly
    if not site_url:
        protocol = settings.PROTOCOL
        hostname = settings.DOMAIN
        port = settings.PORT
        if (protocol, port) in (('https://', 443), ('http://', 80)):
            site_url = ''.join(map(str, (protocol, hostname)))
        else:
            site_url = ''.join(map(str, (protocol, hostname, ':', port)))

    return site_url + url


def chunked(seq, n):
    """
    Yield successive n-sized chunks from seq.

    >>> for group in chunked(range(8), 3):
    ...     print group
    [0, 1, 2]
    [3, 4, 5]
    [6, 7]

    Useful for feeding celery a list of n items at a time.
    """
    seq = iter(seq)
    while 1:
        rv = list(itertools.islice(seq, 0, n))
        if not rv:
            break
        yield rv

# Yanking filters from Django.
register.filter(strip_tags)
register.filter(defaultfilters.timesince)
register.filter(defaultfilters.truncatewords)


@register.function
def thisyear():
    """The current year."""
    return jinja2.Markup(datetime.date.today().year)


@register.function
def url(viewname, *args, **kwargs):
    """Helper for Django's ``reverse`` in templates."""
    return reverse(viewname, args=args, kwargs=kwargs)


@register.filter
def urlparams(url_, hash=None, **query):
    """Add a fragment and/or query paramaters to a URL.

    New query params will be appended to exising parameters, except duplicate
    names, which will be replaced.
    """
    url = urlparse.urlparse(url_)
    fragment = hash if hash is not None else url.fragment

    # Use dict(parse_qsl) so we don't get lists of values.
    q = url.query
    query_dict = dict(urlparse.parse_qsl(smart_str(q))) if q else {}
    query_dict.update((k, v) for k, v in query.items())

    query_string = _urlencode([(k, v) for k, v in query_dict.items()
                               if v is not None])
    new = urlparse.ParseResult(url.scheme, url.netloc, url.path, url.params,
                               query_string, fragment)
    return new.geturl()


def _urlencode(items):
    """A Unicode-safe URLencoder."""
    try:
        return urllib.urlencode(items)
    except UnicodeEncodeError:
        return urllib.urlencode([(k, smart_str(v)) for k, v in items])


@register.filter
def urlencode(txt):
    """Url encode a path."""
    if isinstance(txt, unicode):
        txt = txt.encode('utf-8')
    return urllib.quote_plus(txt)


@register.function
def static(path):
    return staticfiles_storage.url(path)
