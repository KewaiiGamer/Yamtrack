from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0051_user_obfuscate_unseen_episodes"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="public_profile",
            field=models.BooleanField(
                default=False,
                help_text="Allow anyone to view your media tracking profile",
            ),
        ),
    ]
