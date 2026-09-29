def client_ip(request):
    # Never trust forwarded headers unless a controlled proxy is explicitly configured.
    return request.META.get("REMOTE_ADDR")
