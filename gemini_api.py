import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env file.")

genai.configure(api_key=api_key)

def process_audio(audio_path, target_language):
    print("Uploading audio to Gemini...")
    try:
        # Read the audio file directly as bytes
        with open(audio_path, 'rb') as f:
            audio_data = f.read()
            
        # Using gemini-flash-latest for compatibility
        model = genai.GenerativeModel(model_name="models/gemini-flash-latest")
        
        prompt = f"""
        استمع إلى هذا المقطع الصوتي. قم بتفريغه إلى نص بدقة. 
        أكمل الكلمات الناقصة وصحح الأخطاء وصغ التعبيرات لتكون واضحة ومفهومة وسلسة. 
        بعد ذلك، قم بترجمة النص النهائي إلى اللغة '{target_language}'.
        أعطني النص النهائي المترجم والمنسق فقط بدون أي مقدمات أو شروحات إضافية.
        """
        
        print("Waiting for Gemini response...")
        response = model.generate_content([
            prompt, 
            {'mime_type': 'audio/wav', 'data': audio_data}
        ])
        
        text = response.text.strip()
        print(f"Gemini Response: {text}")
        return text
    except Exception as e:
        print(f"Error processing audio with Gemini: {e}")
        return ""
