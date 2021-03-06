from django.conf import settings
from django.contrib.auth.views import logout as auth_logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db import transaction
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import cache_page, never_cache
from django.views.decorators.http import require_POST

from funfactory.helpers import urlparams
from funfactory.urlresolvers import reverse
from tower import ugettext as _
from waffle.decorators import waffle_flag

import mozillians.phonebook.forms as forms
from mozillians.api.models import APIv2App
from mozillians.common.decorators import allow_public, allow_unvouched
from mozillians.common.helpers import redirect
from mozillians.common.middleware import LOGIN_MESSAGE, GET_VOUCHED_MESSAGE
from mozillians.groups.helpers import stringify_groups
from mozillians.groups.models import Group
from mozillians.phonebook.models import Invite
from mozillians.phonebook.utils import redeem_invite
from mozillians.users.managers import EMPLOYEES, MOZILLIANS, PUBLIC, PRIVILEGED
from mozillians.users.models import ExternalAccount, UserProfile, UserProfileMappingType


@allow_unvouched
def login(request):
    if request.user.userprofile.is_complete:
        return redirect('phonebook:home')
    return redirect('phonebook:profile_edit')


@never_cache
@allow_public
def home(request):
    show_start = False
    if request.GET.get('source', ''):
        show_start = True
    return render(request, 'phonebook/home.html',
                  {'show_start': show_start})


@waffle_flag('testing-autovouch-views')
@allow_unvouched
@never_cache
def vouch(request, username):
    """Automatically vouch username.

    This must be behind a waffle flag and activated only for testing
    purposes.

    """
    profile = get_object_or_404(UserProfile, user__username=username)
    now = timezone.now()
    description = 'Automatically vouched for testing purposes on {0}'.format(now)
    vouch = profile.vouch(None, description=description, autovouch=True)
    if vouch:
        messages.success(request, _('Successfully vouched user.'))
    else:
        msg = _('User not vouched. Maybe there are {0} vouches already?')
        msg = msg.format(settings.VOUCH_COUNT_LIMIT)
        messages.error(request, msg)

    return redirect('phonebook:profile_view', profile.user.username)


@waffle_flag('testing-autovouch-views')
@allow_unvouched
@never_cache
def unvouch(request, username):
    """Automatically remove all vouches from username.

    This must be behind a waffle flag and activated only for testing
    purposes.

    """
    profile = get_object_or_404(UserProfile, user__username=username)
    profile.vouches_received.all().delete()
    messages.success(request, _('Successfully unvouched user.'))
    return redirect('phonebook:profile_view', profile.user.username)


@allow_public
@never_cache
def view_profile(request, username):
    """View a profile by username."""
    data = {}
    privacy_mappings = {'anonymous': PUBLIC, 'mozillian': MOZILLIANS, 'employee': EMPLOYEES,
                        'privileged': PRIVILEGED, 'myself': None}
    privacy_level = None

    if (request.user.is_authenticated() and request.user.username == username):
        # own profile
        view_as = request.GET.get('view_as', 'myself')
        privacy_level = privacy_mappings.get(view_as, None)
        profile = UserProfile.objects.privacy_level(privacy_level).get(user__username=username)
        data['privacy_mode'] = view_as
    else:
        userprofile_query = UserProfile.objects.filter(user__username=username)
        public_profile_exists = userprofile_query.public().exists()
        profile_exists = userprofile_query.exists()
        profile_complete = userprofile_query.exclude(full_name='').exists()

        if not public_profile_exists:
            if not request.user.is_authenticated():
                # you have to be authenticated to continue
                messages.warning(request, LOGIN_MESSAGE)
                return (login_required(view_profile, login_url=reverse('phonebook:home'))
                        (request, username))

            if not request.user.userprofile.is_vouched:
                # you have to be vouched to continue
                messages.error(request, GET_VOUCHED_MESSAGE)
                return redirect('phonebook:home')

        if not profile_exists or not profile_complete:
            raise Http404

        profile = UserProfile.objects.get(user__username=username)
        profile.set_instance_privacy_level(PUBLIC)
        if request.user.is_authenticated():
            profile.set_instance_privacy_level(
                request.user.userprofile.privacy_level)

        if (request.user.is_authenticated() and profile.is_vouchable(request.user.userprofile)):

            vouch_form = forms.VouchForm(request.POST or None)
            data['vouch_form'] = vouch_form
            if vouch_form.is_valid():
                # We need to re-fetch profile from database.
                profile = UserProfile.objects.get(user__username=username)
                profile.vouch(request.user.userprofile, vouch_form.cleaned_data['description'])
                # Notify the current user that they vouched successfully.
                msg = _(u'Thanks for vouching for a fellow Mozillian! This user is now vouched!')
                messages.info(request, msg)
                return redirect('phonebook:profile_view', profile.user.username)

    data['shown_user'] = profile.user
    data['profile'] = profile
    data['groups'] = profile.get_annotated_groups()

    # Only show pending groups if user is looking at their own profile,
    # or current user is a superuser
    if not (request.user.is_authenticated() and
            (request.user.username == username or request.user.is_superuser)):
        data['groups'] = [grp for grp in data['groups'] if not grp.pending]

    return render(request, 'phonebook/profile.html', data)


@allow_unvouched
@never_cache
def edit_profile(request):
    """Edit user profile view."""
    # Don't use request.user
    user = User.objects.get(pk=request.user.id)
    profile = user.userprofile
    user_groups = profile.groups.all().order_by('name')
    user_skills = stringify_groups(profile.skills.all().order_by('name'))
    emails = ExternalAccount.objects.filter(type=ExternalAccount.TYPE_EMAIL)
    accounts_qs = ExternalAccount.objects.exclude(type=ExternalAccount.TYPE_EMAIL)

    sections = {
        'registration_section': ['user_form', 'registration_form'],
        'basic_section': ['user_form', 'basic_information_form'],
        'groups_section': ['groups_privacy_form'],
        'skills_section': ['skills_form'],
        'email_section': ['email_privacy_form', 'alternate_email_formset'],
        'languages_section': ['language_privacy_form', 'language_formset'],
        'accounts_section': ['accounts_formset'],
        'irc_section': ['irc_form'],
        'location_section': ['location_form'],
        'contribution_section': ['contribution_form'],
        'tshirt_section': ['tshirt_form'],
        'developer_section': ['developer_form']
    }

    curr_sect = next((s for s in sections.keys() if s in request.POST), None)

    def get_request_data(form):
        if curr_sect and form in sections[curr_sect]:
            return request.POST
        return None

    ctx = {}
    ctx['user_form'] = forms.UserForm(get_request_data('user_form'), instance=user)
    ctx['registration_form'] = forms.RegisterForm(get_request_data('registration_form'),
                                                  request.FILES or None,
                                                  instance=profile)
    basic_information_data = get_request_data('basic_information_form')
    ctx['basic_information_form'] = forms.BasicInformationForm(basic_information_data,
                                                               request.FILES or None,
                                                               instance=profile)
    ctx['accounts_formset'] = forms.AccountsFormset(get_request_data('accounts_formset'),
                                                    instance=profile,
                                                    queryset=accounts_qs)
    ctx['language_formset'] = forms.LanguagesFormset(get_request_data('language_formset'),
                                                     instance=profile,
                                                     locale=request.locale)
    language_privacy_data = get_request_data('language_privacy_form')
    ctx['language_privacy_form'] = forms.LanguagesPrivacyForm(language_privacy_data,
                                                              instance=profile)
    ctx['skills_form'] = forms.SkillsForm(get_request_data('skills_form'), instance=profile,
                                          initial={'skills': user_skills})
    location_initial = {
        'saveregion': True if profile.geo_region else False,
        'savecity': True if profile.geo_city else False,
        'lat': profile.lat,
        'lng': profile.lng
    }
    ctx['location_form'] = forms.LocationForm(get_request_data('location_form'), instance=profile,
                                              initial=location_initial)
    ctx['contribution_form'] = forms.ContributionForm(get_request_data('contribution_form'),
                                                      instance=profile)
    ctx['tshirt_form'] = forms.TshirtForm(get_request_data('tshirt_form'), instance=profile)
    ctx['groups_privacy_form'] = forms.GroupsPrivacyForm(get_request_data('groups_privacy_form'),
                                                         instance=profile)
    ctx['irc_form'] = forms.IRCForm(get_request_data('irc_form'), instance=profile)
    ctx['developer_form'] = forms.DeveloperForm(get_request_data('developer_form'),
                                                instance=profile)
    ctx['email_privacy_form'] = forms.EmailPrivacyForm(get_request_data('email_privacy_form'),
                                                       instance=profile)
    alternate_email_formset_data = get_request_data('alternate_email_formset')
    ctx['alternate_email_formset'] = forms.AlternateEmailFormset(alternate_email_formset_data,
                                                                 instance=profile,
                                                                 queryset=emails)
    forms_valid = True
    if request.POST:
        if not curr_sect:
            raise Http404
        curr_forms = map(lambda x: ctx[x], sections[curr_sect])
        forms_valid = all(map(lambda x: x.is_valid(), curr_forms))
        if forms_valid:
            old_username = request.user.username
            for f in curr_forms:
                f.save()

            if curr_sect == 'registration_section':
                redeem_invite(profile, request.session.get('invite-code'))
            elif user.username != old_username:
                msg = (u'You changed your username; '
                       u'please note your profile URL has also changed.')
                messages.info(request, _(msg))

            next_section = request.GET.get('next')
            next_url = urlparams(reverse('phonebook:profile_edit'), next_section)
            return HttpResponseRedirect(next_url)

    ctx.update({
        'user_groups': user_groups,
        'profile': request.user.userprofile,
        'vouch_threshold': settings.CAN_VOUCH_THRESHOLD,
        'mapbox_id': settings.MAPBOX_PROFILE_ID,
        'apps': user.apiapp_set.filter(is_active=True),
        'appsv2': profile.apps.filter(enabled=True),
        'forms_valid': forms_valid
    })

    return render(request, 'phonebook/edit_profile.html', ctx)


@allow_unvouched
@never_cache
def delete_email(request, email_pk):
    """Delete alternate email address."""
    user = User.objects.get(pk=request.user.id)
    profile = user.userprofile

    # Only email owner can delete emails
    if not ExternalAccount.objects.filter(user=profile, pk=email_pk).exists():
        raise Http404()

    ExternalAccount.objects.get(pk=email_pk).delete()
    return redirect('phonebook:profile_edit')


@allow_unvouched
@never_cache
def change_primary_email(request, email_pk):
    """Change primary email address."""
    user = User.objects.get(pk=request.user.id)
    profile = user.userprofile
    alternate_emails = ExternalAccount.objects.filter(user=profile, type=ExternalAccount.TYPE_EMAIL)

    # Only email owner can change primary email
    if not alternate_emails.filter(pk=email_pk).exists():
        raise Http404()

    alternate_email = alternate_emails.get(pk=email_pk)
    primary_email = user.email

    # Change primary email
    user.email = alternate_email.identifier

    # Turn primary email to alternate
    alternate_email.identifier = primary_email

    with transaction.atomic():
        user.save()
        alternate_email.save()

    return redirect('phonebook:profile_edit')


@allow_unvouched
@never_cache
def confirm_delete(request):
    """Display a confirmation page asking the user if they want to
    leave.

    """
    return render(request, 'phonebook/confirm_delete.html')


@allow_unvouched
@never_cache
@require_POST
def delete(request):
    request.user.delete()
    messages.info(request, _('Your account has been deleted. Thanks for being a Mozillian!'))
    return logout(request)


@allow_public
def search(request):
    limit = None
    people = []
    show_pagination = False
    form = forms.SearchForm(request.GET)
    groups = None
    functional_areas = None

    if form.is_valid():
        query = form.cleaned_data.get('q', u'')
        limit = form.cleaned_data['limit']
        include_non_vouched = form.cleaned_data['include_non_vouched']
        page = request.GET.get('page', 1)
        functional_areas = Group.get_functional_areas()
        public = not (request.user.is_authenticated() and
                      request.user.userprofile.is_vouched)

        profiles = UserProfileMappingType.search(
            query, public=public, include_non_vouched=include_non_vouched)
        if not public:
            groups = Group.search(query)

        paginator = Paginator(profiles, limit)

        try:
            people = paginator.page(page)
        except PageNotAnInteger:
            people = paginator.page(1)
        except EmptyPage:
            people = paginator.page(paginator.num_pages)

        if profiles.count() == 1 and not groups:
            return redirect('phonebook:profile_view', people[0].user.username)

        show_pagination = paginator.count > settings.ITEMS_PER_PAGE

    d = dict(people=people,
             search_form=form,
             limit=limit,
             show_pagination=show_pagination,
             groups=groups,
             functional_areas=functional_areas)

    return render(request, 'phonebook/search.html', d)


@waffle_flag('betasearch')
def betasearch(request):
    """This view is for researching new search and data filtering
    options. It will eventually replace the 'search' view.

    Filtering using SearchFilter does not respect privacy levels
    because it directly hits the db. This should be further
    investigated before the feature is released to the
    public. Meanwhile we limit searches only to vouched users.

    This view is behind the 'betasearch' waffle flag.

    """
    limit = None
    people = []
    show_pagination = False
    form = forms.SearchForm(request.GET)
    filtr = forms.SearchFilter(request.GET)

    if form.is_valid():
        query = form.cleaned_data.get('q', u'')
        limit = form.cleaned_data['limit']
        page = request.GET.get('page', 1)
        public = not (request.user.is_authenticated() and
                      request.user.userprofile.is_vouched)

        profiles_matching_filter = list(filtr.qs.values_list('id', flat=True))
        profiles = UserProfileMappingType.search(query,
                                                 include_non_vouched=True,
                                                 public=public)
        profiles = profiles.filter(id__in=profiles_matching_filter)

        paginator = Paginator(profiles, limit)

        try:
            people = paginator.page(page)
        except PageNotAnInteger:
            people = paginator.page(1)
        except EmptyPage:
            people = paginator.page(paginator.num_pages)

        show_pagination = paginator.count > settings.ITEMS_PER_PAGE

    data = dict(people=people,
                search_form=form,
                filtr=filtr,
                limit=limit,
                show_pagination=show_pagination)

    return render(request, 'phonebook/betasearch.html', data)


@allow_public
@cache_page(60 * 60 * 168)  # 1 week.
def search_plugin(request):
    """Render an OpenSearch Plugin."""
    return render(request, 'phonebook/search_opensearch.xml',
                  content_type='application/opensearchdescription+xml')


def invite(request):
    profile = request.user.userprofile
    invite_form = None
    vouch_form = None
    if profile.can_vouch:
        invite_form = forms.InviteForm(request.POST or None,
                                       instance=Invite(inviter=profile))
        vouch_form = forms.VouchForm(request.POST or None)

    if invite_form and vouch_form and invite_form.is_valid() and vouch_form.is_valid():
        invite_form.instance.reason = vouch_form.cleaned_data['description']
        invite = invite_form.save()
        invite.send(sender=profile, personal_message=invite_form.cleaned_data['message'])
        msg = _(u"%s has been invited to Mozillians. They'll receive an email "
                u"with instructions on how to join. You can "
                u"invite another Mozillian if you like.") % invite.recipient
        messages.success(request, msg)
        return redirect('phonebook:invite')

    return render(request, 'phonebook/invite.html',
                  {
                      'invite_form': invite_form,
                      'vouch_form': vouch_form,
                      'invites': profile.invites.all(),
                      'vouch_threshold': settings.CAN_VOUCH_THRESHOLD,
                  })


@require_POST
def delete_invite(request, invite_pk):
    profile = request.user.userprofile
    deleted_invite = get_object_or_404(Invite, pk=invite_pk, inviter=profile, redeemed=None)
    deleted_invite.delete()

    msg = (_(u"%s's invitation to Mozillians has been revoked. "
             u"You can invite %s again if you like.") %
            (deleted_invite.recipient, deleted_invite.recipient))
    messages.success(request, msg)
    return redirect('phonebook:invite')


@waffle_flag('apiv2')
def apikeys(request):
    profile = request.user.userprofile
    apikey_request_form = forms.APIKeyRequestForm(
        request.POST or None,
        instance=APIv2App(enabled=True, owner=profile)
    )

    if apikey_request_form.is_valid():
        apikey_request_form.save()
        msg = _(u'API Key generated successfully.')
        messages.success(request, msg)
        return redirect('phonebook:apikeys')

    data = {
        'apps': request.user.apiapp_set.filter(is_active=True),
        'appsv2': profile.apps.filter(enabled=True),
        'apikey_request_form': apikey_request_form,
    }
    return render(request, 'phonebook/apikeys.html', data)


@waffle_flag('apiv2')
def delete_apikey(request, api_pk):
    api_key = get_object_or_404(APIv2App, pk=api_pk, owner=request.user.userprofile)
    api_key.delete()
    messages.success(request, _('API key successfully deleted.'))
    return redirect('phonebook:apikeys')


def list_mozillians_in_location(request, country, region=None, city=None):
    queryset = UserProfile.objects.vouched().filter(geo_country__name__iexact=country)
    show_pagination = False

    if city:
        queryset = queryset.filter(geo_city__name__iexact=city)
    if region:
        queryset = queryset.filter(geo_region__name__iexact=region)

    paginator = Paginator(queryset, settings.ITEMS_PER_PAGE)
    page = request.GET.get('page', 1)

    try:
        people = paginator.page(page)
    except PageNotAnInteger:
        people = paginator.page(1)
    except EmptyPage:
        people = paginator.page(paginator.num_pages)

    if paginator.count > settings.ITEMS_PER_PAGE:
        show_pagination = True

    data = {'people': people,
            'country_name': country,
            'city_name': city,
            'region_name': region,
            'page': page,
            'show_pagination': show_pagination}
    return render(request, 'phonebook/location_list.html', data)


@allow_unvouched
def logout(request):
    """View that logs out the user and redirects to home page."""
    auth_logout(request)
    return redirect('phonebook:home')


@allow_public
def register(request):
    """Registers Users.

    Pulls out an invite code if it exists and auto validates the user
    if so. Single-purpose view.
    """
    # TODO already vouched users can be re-vouched?
    if 'code' in request.GET:
        request.session['invite-code'] = request.GET['code']
        if request.user.is_authenticated():
            if not request.user.userprofile.is_vouched:
                redeem_invite(request.user.userprofile, request.session['invite-code'])
        else:
            messages.info(request, _("You've been invited to join Mozillians.org! "
                                     "Sign in and then you can create a profile."))

    return redirect('phonebook:home')
