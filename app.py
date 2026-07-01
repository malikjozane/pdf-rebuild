from flask import Flask, render_template_string, request, send_file, flash, redirect
import requests
import os
import tempfile
import uuid
from rebuild_runsheet import process_pdf

app = Flask(__name__)
app.secret_key = "super_secret_key_change_in_production"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PDF Runsheet Optimizer</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #4F46E5;
            --primary-hover: #4338CA;
            --bg-grad-1: #1e1b4b;
            --bg-grad-2: #312e81;
            --glass-bg: rgba(255, 255, 255, 0.05);
            --glass-border: rgba(255, 255, 255, 0.1);
            --text-color: #f3f4f6;
        }

        body {
            font-family: 'Inter', sans-serif;
            margin: 0;
            padding: 0;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            background: linear-gradient(135deg, var(--bg-grad-1), var(--bg-grad-2));
            color: var(--text-color);
            position: relative;
            overflow: hidden;
        }

        /* Abstract Background Elements */
        .bg-circle-1 {
            position: absolute;
            width: 400px;
            height: 400px;
            background: rgba(79, 70, 229, 0.4);
            border-radius: 50%;
            top: -100px;
            left: -100px;
            filter: blur(80px);
            z-index: 0;
        }
        .bg-circle-2 {
            position: absolute;
            width: 300px;
            height: 300px;
            background: rgba(236, 72, 153, 0.3);
            border-radius: 50%;
            bottom: -50px;
            right: -50px;
            filter: blur(80px);
            z-index: 0;
        }

        .container {
            position: relative;
            z-index: 10;
            background: var(--glass-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 3rem;
            width: 90%;
            max-width: 600px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            text-align: center;
        }

        h1 {
            margin-top: 0;
            font-weight: 700;
            font-size: 2rem;
            background: linear-gradient(to right, #a5b4fc, #fbcfe8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }

        p {
            color: #9ca3af;
            font-size: 1rem;
            margin-bottom: 2rem;
            line-height: 1.5;
        }

        form {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .input-group {
            position: relative;
        }

        input[type="url"] {
            width: 100%;
            padding: 1rem 1.5rem;
            border-radius: 12px;
            border: 1px solid var(--glass-border);
            background: rgba(0, 0, 0, 0.2);
            color: white;
            font-size: 1rem;
            box-sizing: border-box;
            outline: none;
            transition: all 0.3s ease;
        }

        input[type="url"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.2);
            background: rgba(0, 0, 0, 0.4);
        }

        input[type="url"]::placeholder {
            color: #6b7280;
        }

        button {
            background: var(--primary);
            color: white;
            border: none;
            padding: 1rem;
            border-radius: 12px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
        }

        button:hover {
            background: var(--primary-hover);
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.4);
        }

        button:active {
            transform: translateY(0);
        }

        .loader {
            display: none;
            width: 20px;
            height: 20px;
            border: 3px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: white;
            animation: spin 1s ease-in-out infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .alert {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #fca5a5;
            padding: 1rem;
            border-radius: 12px;
            margin-bottom: 1.5rem;
            font-size: 0.95rem;
        }

        /* SVG Icon for Button */
        .btn-icon {
            width: 20px;
            height: 20px;
            fill: none;
            stroke: currentColor;
            stroke-width: 2;
            stroke-linecap: round;
            stroke-linejoin: round;
        }

    </style>
</head>
<body>
    <div class="bg-circle-1"></div>
    <div class="bg-circle-2"></div>

    <div class="container">
        <h1>Runsheet Optimizer</h1>
        <p>Paste the direct URL of your original Shipox PDF. We'll extract, compress, and reformat it into a beautiful single-page printout with Arabic support.</p>

        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="alert">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <form method="POST" action="/process" id="processForm">
            <div class="input-group">
                <input type="url" name="pdf_url" placeholder="https://..." required autocomplete="off">
            </div>
            <button type="submit" id="submitBtn">
                <span id="btnText">Optimize PDF</span>
                <svg class="btn-icon" id="btnIcon" viewBox="0 0 24 24">
                    <path d="M5 12h14M12 5l7 7-7 7"/>
                </svg>
                <div class="loader" id="loader"></div>
            </button>
        </form>
    </div>

    <script>
        const form = document.getElementById('processForm');
        const btn = document.getElementById('submitBtn');
        const btnText = document.getElementById('btnText');
        const btnIcon = document.getElementById('btnIcon');
        const loader = document.getElementById('loader');

        form.addEventListener('submit', function() {
            btnText.textContent = 'Processing...';
            btnIcon.style.display = 'none';
            loader.style.display = 'block';
            btn.style.pointerEvents = 'none';
            btn.style.opacity = '0.8';
            
            // Reset button state after a delay assuming download starts
            setTimeout(() => {
                btnText.textContent = 'Optimize PDF';
                btnIcon.style.display = 'block';
                loader.style.display = 'none';
                btn.style.pointerEvents = 'auto';
                btn.style.opacity = '1';
            }, 6000);
        });
    </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/process", methods=["POST"])
def process():
    pdf_url = request.form.get("pdf_url")
    if not pdf_url:
        flash("Please provide a valid PDF URL.")
        return redirect("/")

    try:
        # Download the PDF
        response = requests.get(pdf_url, stream=True, timeout=15)
        response.raise_for_status()

        # Save to a temporary file
        temp_dir = tempfile.gettempdir()
        unique_id = str(uuid.uuid4())
        input_pdf_path = os.path.join(temp_dir, f"input_{unique_id}.pdf")
        output_pdf_path = os.path.join(temp_dir, f"optimized_{unique_id}.pdf")

        with open(input_pdf_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        # Process the PDF
        process_pdf(input_pdf_path, output_pdf_path)

        # Send the file to the user
        return send_file(
            output_pdf_path,
            as_attachment=True,
            download_name="Optimized_RunSheet.pdf",
            mimetype="application/pdf"
        )

    except requests.exceptions.RequestException as e:
        flash(f"Error downloading the PDF: {e}")
        return redirect("/")
    except Exception as e:
        flash(f"Error processing the PDF: {str(e)}")
        return redirect("/")
    finally:
        pass

if __name__ == "__main__":
    app.run(debug=True, port=5000)
