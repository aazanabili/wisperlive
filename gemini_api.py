import google.generativeai as genai

def process_audio(audio_path, target_language, api_key):
    if not api_key:
        raise ValueError("API key is missing. Please enter your Gemini API key in the settings.")

    genai.configure(api_key=api_key)

    print("Processing audio with Gemini...")
    try:
        with open(audio_path, 'rb') as f:
            audio_data = f.read()

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
        raise
