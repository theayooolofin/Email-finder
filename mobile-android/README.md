# Lead Finder Android APK

This is a WebView Android app that opens your Lead Finder backend URL.

## 1. Prerequisites
- Android Studio (latest stable)
- Android SDK Platform 35
- JDK 17 (bundled in Android Studio)

## 2. Configure Backend URL
Edit `mobile-android/gradle.properties`:

```properties
BACKEND_URL=https://your-backend-url.com/
```

Examples:
- Emulator + local backend: `http://10.0.2.2:8000/`
- Physical phone + local backend: `http://<YOUR_PC_LAN_IP>:8000/`

If using local backend for phone, run FastAPI with:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 3. Build APK in Android Studio
1. Open Android Studio.
2. `Open` -> select `mobile-android/`.
3. Let Gradle sync complete.
4. `Build` -> `Build Bundle(s) / APK(s)` -> `Build APK(s)`.

Debug APK output:
- `mobile-android/app/build/outputs/apk/debug/app-debug.apk`

## 4. Install on Phone
- Enable `Developer options` + `USB debugging` on phone.
- Connect phone and run:

```powershell
adb install -r mobile-android\\app\\build\\outputs\\apk\\debug\\app-debug.apk
```

Or copy the APK to phone and install manually.

## 5. Release APK (for sharing)
In Android Studio:
1. `Build` -> `Generate Signed Bundle / APK`.
2. Choose `APK`.
3. Create/select keystore.
4. Build `release`.
