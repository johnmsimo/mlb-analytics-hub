$(python3 -c "
with open('/tmp/app_patched.py', 'r', encoding='utf-8') as f:
    print(f.read(), end='')
")