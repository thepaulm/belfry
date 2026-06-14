plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
    // Firebase: must come after the Android/Flutter plugins. Requires
    // app/google-services.json (Track A — gitignored, drop in your own).
    id("com.google.gms.google-services")
}

android {
    namespace = "io.yellowchicken.belfry"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
        // flutter_local_notifications >= 18 uses java.time APIs that need
        // desugaring to run below API 26.
        isCoreLibraryDesugaringEnabled = true
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_11.toString()
    }

    defaultConfig {
        applicationId = "io.yellowchicken.belfry"
        // google_sign_in v7+ uses Android Credential Manager (API 23+).
        // Flutter's default minSdk is now 24, which already clears that bar,
        // so we track the default rather than hardcoding 23 — that also stops
        // the Gradle migrator from rewriting this line every build.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

flutter {
    source = "../.."
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")
}
