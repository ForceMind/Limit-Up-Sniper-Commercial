import codecs
f = codecs.open('frontend/index.html', 'r', 'utf-8').read()
div_open = f.count('<div')
div_close = f.count('</div>')
script_open = f.count('<script')
script_close = f.count('</script>')
print(f"<div>: {div_open}, </div>: {div_close}")
print(f"<script>: {script_open}, </script>: {script_close}")
