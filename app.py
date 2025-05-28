# =============================
# CONFIGURACIÓN Y DEPENDENCIAS
# =============================
from flask import Flask, render_template, request, jsonify, url_for, redirect, session
import os
from dotenv import load_dotenv
import json
import time
import threading
import queue
from google import genai

# Carga variables de entorno desde .env (clave de API, etc.)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =============================
# PROMPT PARA GEMINI
# =============================
PROMT = """
Eres un generador experto de ejercicios de análisis de código en PSeInt para estudiantes. Tu tarea es crear preguntas de opción múltiple basadas en fragmentos de código PSeInt, siguiendo estas reglas estrictas:

1. El código debe estar escrito en PSeInt válido y claro, usando la sintaxis oficial.
2. Si usas funciones o subprocesos, DEBEN estar definidos SIEMPRE fuera del algoritmo principal, nunca anidados ni dentro del algoritmo principal.
3. El código debe usar solo estructuras permitidas: condicionales, ciclos, subprogramas, listas unidimensionales (NUNCA uses arreglos de más de una dimensión). Si quieres puede combinar varios en el mismo programa.
4. El código debe estar correctamente indentado y sin errores de sintaxis.
5. No incluyas comentarios, explicaciones ni texto adicional fuera del objeto JSON.
6. El fragmento de código debe ser autocontenible y ejecutable en PSeInt, sin dependencias externas.
7. Las variables deben estar correctamente definidas y declaradas y usadas según las reglas de PSeInt.
8. El código debe ser lo más claro y didáctico posible, evitando ambigüedades.
9. Cada pregunta debe usar un programa y opciones de respuesta diferentes a las preguntas generadas anteriormente en la misma sesión.
10. Para la asignación de valores, en la primera línea de definición de funciones o subprocesos (por ejemplo: "Funcion resultado <- suma(a, b)"), usa la flecha (<-). En el resto del código, incluido el interior de funciones y subprocesos, usa siempre el signo igual (=) para asignación. Para la comparación de valores usa dos signos iguales (==).

Genera preguntas variadas de estos dos tipos (elige aleatoriamente en cada generación):
- ¿Qué salida tendrá el siguiente código?
- ¿Qué salida tendrá el siguiente código si se ingresan los siguientes valores? (en este caso, incluye en la pregunta los valores de entrada y asegúrate de que el código use Leer)

El formato de la respuesta debe ser un ÚNICO objeto JSON, SIN ningún texto antes o después, ni bloques de código Markdown. El objeto debe tener exactamente estas claves:

{
  "Pregunta": "Texto de la pregunta clara y concisa.",
  "Codigo": "Fragmento de código PSeInt válido y autocontenible.",
  "Respuestas": ["Respuesta A", "Respuesta B", "Respuesta C", "Respuesta D"],
  "Respuesta correcta": "Respuesta correcta exactamente igual a una de las opciones",
  "Explicacion": "Explicación breve y genérica de por qué la respuesta correcta es la correcta, sin hacer referencia a la opción elegida por el usuario, sino explicando el razonamiento o el resultado del código."
}

Recuerda: responde SOLO con el objeto JSON, sin bloques de código, sin explicaciones y sin texto adicional. El código debe ser válido y ejecutable en PSeInt, con funciones y subprocesos SIEMPRE fuera del algoritmo principal.
"""

# =============================
# CLIENTE GEMINI
# =============================
client = genai.Client(api_key=GEMINI_API_KEY)

# =============================
# CACHE DE PREGUNTAS (COLA)
# =============================
CACHE_SIZE = 40  # Máximo de preguntas en cache
CACHE_MIN = 10   # Umbral mínimo para reponer el cache
pregunta_cache = queue.Queue(maxsize=CACHE_SIZE)
cache_lock = threading.Lock()  # (No se usa, pero útil si se extiende)

# =============================
# HILO DE PRECARGA DE PREGUNTAS
# =============================
def precargar_preguntas():
    """
    Hilo en segundo plano que mantiene el cache de preguntas lleno.
    Solo consulta la API si el cache baja del umbral.
    """
    while True:
        if pregunta_cache.qsize() < CACHE_MIN:
            try:
                pregunta = generar_pregunta()
                # Solo cachea si es válida
                if isinstance(pregunta, dict) and 'pregunta' in pregunta and 'codigo' in pregunta:
                    pregunta_cache.put(pregunta)
            except Exception:
                pass  # Silencia errores para no detener el hilo
        else:
            time.sleep(2)  # Espera antes de volver a chequear

# Inicia el hilo de precarga al arrancar la app
threading.Thread(target=precargar_preguntas, daemon=True).start()

# =============================
# GENERACIÓN Y OBTENCIÓN DE PREGUNTAS
# =============================
def generar_pregunta():
    """
    Llama a Gemini para generar una pregunta nueva.
    Limpia el texto y lo convierte a un diccionario Python.
    """
    response = client.models.generate_content(
        model="gemini-2.0-flash", 
        contents=PROMT
    )
    try:
        text = response.text.strip()
        # Limpieza de posibles bloques de código
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        pregunta_json = json.loads(text)
        # Si "Respuestas" es una lista, úsala directamente. Si es string, sepáralo por comas.
        respuestas = pregunta_json.get("Respuestas")
        if isinstance(respuestas, str):
            respuestas = [r.strip() for r in respuestas.split(",")]
        elif not isinstance(respuestas, list):
            respuestas = []
        pregunta = {
            "pregunta": pregunta_json.get("Pregunta"),
            "codigo": pregunta_json.get("Codigo"),
            "respuestas": respuestas,
            "respuesta_correcta": pregunta_json.get("Respuesta correcta"),
            "explicacion": pregunta_json.get("Explicacion", "")
        }
        return pregunta
    except Exception as e:
        # Si falla el parseo, devuelve un error para mostrarlo en la app
        return {"error": "No se pudo extraer el JSON", "detalle": str(e), "texto": response.text}

def obtener_pregunta_cache():
    """
    Obtiene una pregunta del cache (espera hasta 10s).
    Si el cache está vacío, genera una pregunta en caliente.
    """
    try:
        return pregunta_cache.get(timeout=10)
    except Exception:
        return generar_pregunta()

# =============================
# FLASK APP Y RUTAS
# =============================
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Necesario para usar sesiones

@app.route('/')
def inicio():
    """
    Ruta de inicio: muestra la presentación y botón para comenzar el quiz.
    """
    return render_template('inicio.html')

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    """
    Ruta principal del quiz: muestra la pregunta actual y procesa la respuesta.
    """
    if 'puntaje' not in session:
        # Inicializa la sesión si entra directo
        session['puntaje'] = 0
        session['total'] = 0
        session['inicio'] = time.time()
        session['pregunta_actual'] = obtener_pregunta_cache()
        session['errores'] = []

    if request.method == 'POST':
        seleccion = request.form.get('respuesta')
        correcta = session['pregunta_actual']['respuesta_correcta']
        explicacion = session['pregunta_actual'].get('explicacion', '')
        session['total'] += 1
        if seleccion and seleccion.strip() == correcta.strip():
            session['puntaje'] += 1
        else:
            errores = session.get('errores', [])
            errores.append({
                'pregunta': session['pregunta_actual']['pregunta'],
                'codigo': session['pregunta_actual']['codigo'],
                'respuestas': session['pregunta_actual']['respuestas'],
                'respuesta_correcta': correcta,
                'explicacion': explicacion,
                'respuesta_usuario': seleccion
            })
            session['errores'] = errores

        if session['total'] >= 5:
            tiempo = int(time.time() - session['inicio'])
            puntaje = session['puntaje']
            errores = session.get('errores', [])
            session.clear()  # Limpia la sesión para un nuevo intento
            return render_template('resultado.html', correctas=puntaje, tiempo=tiempo, errores=errores)

        session['pregunta_actual'] = obtener_pregunta_cache()

    pregunta = session['pregunta_actual']
    num_pregunta = session.get('total', 0) + 1
    return render_template('quiz.html', pregunta=pregunta, num_pregunta=num_pregunta)

@app.route('/resultado')
def resultado():
    """
    Ruta para mostrar el resultado final (permite refrescar la página de resultado).
    """
    # Si se accede directamente, intenta recuperar errores de la sesión
    errores = session.get('errores', [])
    return render_template('resultado.html', 
                           correctas=request.args.get('correctas', 0), 
                           tiempo=request.args.get('tiempo', 0), 
                           errores=errores)

@app.route('/error')
def error():
    """
    Ruta para mostrar errores personalizados.
    """
    detalle = request.args.get('detalle', 'Error desconocido')
    texto = request.args.get('texto', '')
    return render_template('error.html', detalle=detalle, texto=texto), 500

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)