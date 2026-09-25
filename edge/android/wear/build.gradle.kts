plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// The Samsung Health Sensor SDK is licensed separately. Drop its .aar into wear/libs/ to enable the Galaxy Watch
// source (SpO2, IBI/HRV, skin temperature); without it the app builds with Health Services + scripted sources.
val samsungAars = fileTree("libs") { include("*.aar") }
val hasSamsungSdk = samsungAars.files.isNotEmpty()

android {
    namespace = "com.cloudbox.rpm.wear"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.cloudbox.rpm.wear"
        minSdk = 30
        targetSdk = 34
        versionCode = 3
        versionName = "0.3.0"
        buildConfigField("boolean", "HAS_SAMSUNG_SDK", hasSamsungSdk.toString())
        buildConfigField("String", "DEFAULT_MQTT_URI", "\"tcp://10.0.2.2:1883\"")   // emulator -> host Mosquitto
        buildConfigField("String", "DEFAULT_DEVICE_ID", "\"gw-demo-01\"")
    }
    sourceSets["main"].java.srcDir(if (hasSamsungSdk) "src/samsung/kotlin" else "src/nosamsung/kotlin")
    buildFeatures { compose = true; buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
    packaging { resources { excludes += setOf("META-INF/*.kotlin_module", "META-INF/versions/9/previous-compilation-data.bin") } }
}

dependencies {
    implementation(project(":shared"))
    if (hasSamsungSdk) implementation(samsungAars)
    implementation(libs.core.ktx)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.service)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.wear.compose.material)
    implementation(libs.wear.compose.foundation)
    implementation(libs.health.services)
    implementation(libs.play.services.wearable)
    implementation(libs.coroutines.android)
    implementation(libs.coroutines.play.services)
    implementation(libs.coroutines.guava)
}
