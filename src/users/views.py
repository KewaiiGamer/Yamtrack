import logging

import apprise
from django.conf import settings
from django.apps import apps
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Avg, Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_celery_beat.models import PeriodicTask

from app.models import BasicMedia, Episode, Item, MediaTypes, Status
from app.providers import tmdb
from app.templatetags import app_tags
from users.forms import NotificationSettingsForm, PasswordChangeForm, UserUpdateForm
from users.models import (
    DateFormatChoices,
    MediaSortChoices,
    MediaStatusChoices,
    QuickWatchDateChoices,
    TimeFormatChoices,
    User,
)

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def account(request):
    """Update the user's account and import/export data."""
    user_form = UserUpdateForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == "POST":
        # Handle username update
        if "username" in request.POST:
            user_form = UserUpdateForm(request.POST, instance=request.user)

            if user_form.is_valid():
                user_form.save()
                messages.success(request, "Your username has been updated!")
                logger.info(
                    "Successful username change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed username change for user: %s - %s",
                request.user.username,
                list(user_form.errors.keys()),
            )

        # Handle password update
        elif any(
            key in request.POST
            for key in ["old_password", "new_password1", "new_password2"]
        ):
            password_form = PasswordChangeForm(user=request.user, data=request.POST)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(
                    request,
                    user,
                )
                messages.success(request, "Your password has been updated!")
                logger.info(
                    "Successful password change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed password change for user: %s - %s",
                request.user.username,
                list(password_form.errors.keys()),
            )

    context = {
        "user_form": user_form,
        "password_form": password_form,
    }

    return render(request, "users/account.html", context)


@require_http_methods(["GET", "POST"])
def notifications(request):
    """Render the notifications settings page."""
    if request.method == "POST":
        form = NotificationSettingsForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification settings updated successfully!")
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, f"{error}")

        return redirect("notifications")

    form = NotificationSettingsForm(instance=request.user)

    return render(
        request,
        "users/notifications.html",
        {
            "form": form,
        },
    )


@require_GET
def search_items(request):
    """Search for items to exclude from notifications."""
    query = request.GET.get("q", "").strip()

    if not query or len(query) <= 1:
        return render(
            request,
            "users/components/search_results.html",
        )

    # Search for items that match the query
    items = (
        Item.objects.filter(
            Q(title__icontains=query),
        )
        .exclude(
            id__in=request.user.notification_excluded_items.values_list(
                "id",
                flat=True,
            ),
        )
        .distinct()[:10]
    )

    return render(
        request,
        "users/components/search_results.html",
        {"items": items, "query": query},
    )


@require_POST
def exclude_item(request):
    """Exclude an item from notifications."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.add(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()

    return render(
        request,
        "users/components/excluded_items.html",
        {"excluded_items": excluded_items},
    )


@require_POST
def include_item(request):
    """Remove an item from the exclusion list."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.remove(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()

    return render(
        request,
        "users/components/excluded_items.html",
        {"excluded_items": excluded_items},
    )


@require_GET
def test_notification(request):
    """Send a test notification to the user."""
    try:
        # Create Apprise instance
        apobj = apprise.Apprise()

        # Add all notification URLs
        notification_urls = [
            url.strip()
            for url in request.user.notification_urls.splitlines()
            if url.strip()
        ]
        if not notification_urls:
            messages.error(request, "No notification URLs configured.")
            return redirect("notifications")

        for url in notification_urls:
            apobj.add(url)

        # Send test notification
        result = apobj.notify(
            title="YamTrack Test Notification",
            body=(
                "This is a test notification from YamTrack. "
                "If you're seeing this, your notifications are working correctly!"
            ),
        )

        if result:
            messages.success(request, "Test notification sent successfully!")
        else:
            messages.error(request, "Failed to send test notification.")
    except Exception:
        logger.exception("Error sending notification")

    return redirect("notifications")


@require_http_methods(["GET", "POST"])
def preferences(request):
    """Render the preferences settings page."""
    media_types = MediaTypes.values
    media_types.remove(MediaTypes.EPISODE.value)
    watch_provider_regions = tmdb.watch_provider_regions()

    if request.method == "GET":
        public_profile_url = request.build_absolute_uri(
            reverse("public_profile", args=[request.user.username]),
        )
        return render(
            request,
            "users/preferences.html",
            {
                "media_types": media_types,
                "quick_watch_date_choices": QuickWatchDateChoices.choices,
                "date_format_choices": DateFormatChoices.choices,
                "time_format_choices": TimeFormatChoices.choices,
                "watch_provider_choices": watch_provider_regions,
                "public_profile_url": public_profile_url,
            },
        )

    # Prevent demo users from updating preferences
    if request.user.is_demo:
        messages.error(request, "This section is view-only for demo accounts.")
        return redirect("preferences")

    # Process form submission
    request.user.clickable_media_cards = "clickable_media_cards" in request.POST
    request.user.obfuscate_unseen_episodes = "obfuscate_unseen_episodes" in request.POST
    request.user.quick_watch_date = request.POST.get(
        "quick_watch_date",
        QuickWatchDateChoices.CURRENT_DATE,
    )
    request.user.progress_bar = "progress_bar" in request.POST
    request.user.hide_completed_recommendations = (
        "hide_completed_recommendations" in request.POST
    )
    request.user.hide_zero_rating = "hide_zero_rating" in request.POST
    request.user.public_profile = "public_profile" in request.POST
    request.user.date_format = request.POST.get(
        "date_format",
        DateFormatChoices.ISO,
    )
    request.user.time_format = request.POST.get(
        "time_format",
        TimeFormatChoices.HOUR_24,
    )
    media_types_checked = request.POST.getlist("media_types_checkboxes")

    provider_region = request.POST.get("watch_provider_region", "")
    if provider_region in [region[0] for region in watch_provider_regions]:
        request.user.watch_provider_region = provider_region
    else:
        request.user.watch_provider_region = "UNSET"

    # Update user preferences for each media type
    for media_type in media_types:
        setattr(
            request.user,
            f"{media_type}_enabled",
            media_type in media_types_checked,
        )

    # Save changes and redirect
    request.user.save()
    messages.success(request, "Settings updated.")

    return redirect("preferences")


@require_GET
def integrations(request):
    """Render the integrations settings page."""
    return render(request, "users/integrations.html")


@require_GET
def import_data(request):
    """Render the import data settings page."""
    import_tasks = request.user.get_import_tasks()
    return render(request, "users/import_data.html", {"import_tasks": import_tasks})


@require_GET
def export_data(request):
    """Render the export data settings page."""
    return render(request, "users/export_data.html")


@require_GET
def advanced(request):
    """Render the advanced settings page."""
    return render(request, "users/advanced.html")


@require_GET
def about(request):
    """Render the about page."""
    return render(request, "users/about.html", {"version": settings.VERSION})


@require_POST
def delete_import_schedule(request):
    """Delete an import schedule."""
    task_name = request.POST.get("task_name")
    try:
        task = PeriodicTask.objects.get(
            name=task_name,
            kwargs__contains=f'"user_id": {request.user.id}',
        )
        task.delete()
        messages.success(request, "Import schedule deleted.")
    except PeriodicTask.DoesNotExist:
        messages.error(request, "Import schedule not found.")
    return redirect("import_data")


@require_POST
def regenerate_token(request):
    """Regenerate the token for the user."""
    while True:
        try:
            request.user.regenerate_token()
            messages.success(request, "Token regenerated successfully.")
            break
        except IntegrityError:
            continue
    return redirect("integrations")


@require_POST
def update_plex_usernames(request):
    """Update the Plex usernames for the user."""
    usernames = request.POST.get("plex_usernames", "")

    username_list = [u.strip() for u in usernames.split(",") if u.strip()]

    seen = set()
    deduplicated_usernames = [
        u for u in username_list if not (u in seen or seen.add(u))
    ]

    # Reconstruct with comma-space separation
    cleaned_usernames = ", ".join(deduplicated_usernames)

    if cleaned_usernames != request.user.plex_usernames:
        request.user.plex_usernames = cleaned_usernames
        request.user.save(update_fields=["plex_usernames"])
        messages.success(request, "Plex usernames updated successfully")

    return redirect("integrations")


@require_POST
def clear_search_cache(request):
    """Clear all cached search entries."""
    deleted = cache.delete_pattern("search_*")

    messages.success(
        request,
        f"Successfully cleared {deleted} search entr{pluralize(deleted, 'y,ies')}",
    )
    logger.info(
        "Successfully cleared %s search entries",
        deleted,
    )

    return redirect("advanced")


@login_not_required
@require_GET
def public_profile(request, username):
    """Display a user's public profile with their media tracking."""
    profile_user = get_object_or_404(User, username=username)

    if not profile_user.public_profile:
        raise Http404

    media_type = request.GET.get("media_type")
    status_filter = request.GET.get("status", MediaStatusChoices.ALL)
    sort_filter = request.GET.get("sort", MediaSortChoices.SCORE)
    page = request.GET.get("page", 1)

    enabled_types = profile_user.get_enabled_media_types()
    # Exclude season/episode from the public profile tabs
    display_types = [
        t for t in enabled_types
        if t not in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value)
    ]

    if not display_types:
        raise Http404

    # Default to first enabled type if none specified or invalid
    if media_type not in display_types:
        media_type = display_types[0]

    # Validate sort and status
    if sort_filter not in MediaSortChoices.values:
        sort_filter = MediaSortChoices.SCORE
    if status_filter not in MediaStatusChoices.values:
        status_filter = MediaStatusChoices.ALL

    media_queryset = BasicMedia.objects.get_media_list(
        user=profile_user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
    )

    items_per_page = 32
    paginator = Paginator(media_queryset, items_per_page)
    media_page = paginator.get_page(page)

    BasicMedia.objects.annotate_max_progress(
        media_page.object_list,
        media_type,
    )

    # Compute profile statistics
    stats = {}
    for mt in display_types:
        model = apps.get_model(app_label="app", model_name=mt)
        qs = model.objects.filter(user=profile_user)
        total = qs.count()
        completed = qs.filter(status=Status.COMPLETED.value).count()
        avg_score = qs.exclude(score__isnull=True).exclude(score=0).aggregate(
            avg=Avg("score"),
        )["avg"]
        stats[mt] = {
            "total": total,
            "completed": completed,
            "avg_score": round(float(avg_score), 1) if avg_score else None,
        }

    # Total episodes watched
    episodes_watched = Episode.objects.filter(
        related_season__user=profile_user,
        end_date__isnull=False,
    ).count()

    # Overall average score across all types
    score_sums = []
    score_counts = []
    for mt in display_types:
        model = apps.get_model(app_label="app", model_name=mt)
        agg = model.objects.filter(
            user=profile_user,
        ).exclude(score__isnull=True).exclude(score=0).aggregate(
            avg=Avg("score"), cnt=Count("score"),
        )
        if agg["avg"] and agg["cnt"]:
            score_sums.append(float(agg["avg"]) * agg["cnt"])
            score_counts.append(agg["cnt"])

    total_score_count = sum(score_counts)
    overall_avg = (
        round(sum(score_sums) / total_score_count, 1)
        if total_score_count > 0 else None
    )

    total_completed = sum(s["completed"] for s in stats.values())
    total_items = sum(s["total"] for s in stats.values())

    # Watch time estimates (runtimes not stored in DB)
    from app.models import Movie
    completed_movies = Movie.objects.filter(
        user=profile_user,
        status=Status.COMPLETED.value,
    ).count()
    # Estimate: average movie ~2 hours
    movie_hours = round(completed_movies * 2)

    # TV watch time: episodes * average episode runtime (~30 min)
    tv_hours = round(episodes_watched * 30 / 60)

    context = {
        "profile_user": profile_user,
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "display_types": display_types,
        "current_sort": sort_filter,
        "current_status": status_filter,
        "sort_choices": MediaSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
        "stats": stats,
        "episodes_watched": episodes_watched,
        "overall_avg_score": overall_avg,
        "total_completed": total_completed,
        "total_items": total_items,
        "movie_hours": movie_hours,
        "tv_hours": tv_hours,
    }

    if request.headers.get("HX-Request"):
        return render(request, "users/components/public_profile_grid.html", context)

    return render(request, "users/public_profile.html", context)
