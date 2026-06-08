// Main JS handling UI interactions

document.addEventListener('DOMContentLoaded', () => {
    // Add slide-in animations to flash messages
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.classList.add('fade');
            alert.classList.remove('show');
            setTimeout(() => alert.remove(), 150);
        }, 5000); // Auto dismiss after 5s
    });
});
