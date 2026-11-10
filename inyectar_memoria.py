import re 
import datetime 
def guardar_estudio_local(titulo, analisis): 
    timestamp = datetime.datetime.now().strftime("%%Y-%%m-%%d %%H:%%M:%%S") 
    entrada = f"\n\n## [{timestamp}] {titulo}\n{analisis}\n" 
    with open("memoria_razonamiento.md", "a", encoding="utf-8") as memoria: 
        memoria.write(entrada) 
with open("bot_weex.py", "r", encoding="utf-8") as f: content = f.read() 
if "def guardar_estudio_local" not in content: content = codigo_a_insertar + "\n" + content 
content = re.sub(r'(logger\.info\(f"Centro aprendizaje:\s*\{\s*res\s*\}"\))', r'guardar_estudio_local("Centro de Aprendizaje", f"Resultados: {res}")\n    \1', content) 
with open("bot_weex.py", "w", encoding="utf-8") as f: f.write(content) 
