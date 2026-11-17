from django import forms
from django.contrib import admin
from django.contrib.auth.hashers import make_password

from .models import User


class UserAdminForm(forms.ModelForm):
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(render_value=False),
        min_length=6,
        required=False,
        help_text="Enter a new password when creating or changing this account.",
    )

    class Meta:
        model = User
        fields = ("username", "email", "password", "role")

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.password = make_password(password)
        if commit:
            user.save()
        return user


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    form = UserAdminForm
    list_display = ("username", "email", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("username", "email")
    readonly_fields = ("created_at",)
