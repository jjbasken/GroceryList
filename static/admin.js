document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.delete-user-form').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            var username = form.dataset.username;
            if (!confirm('Delete user ' + username + '? This cannot be undone.')) {
                e.preventDefault();
            }
        });
    });
});
