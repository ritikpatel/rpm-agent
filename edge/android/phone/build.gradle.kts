plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.cloudbox.rpm.phone"
    compileSdk = 35
    defaultConfig {
        // Same applicationId family as the watch app is not required, but both must be signed with the same key for
        // the Wearable Data Layer to deliver messages between them.
        applicationId = "com.cloudbox.rpm.phone"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "0.3.0"
        buildConfigField("String", "DEFAULT_MQTT_URI", "\"tcp://10.0.2.2:1883\"")
        buildConfigField("String", "DEFAULT_DEVICE_ID", "\"gw-demo-01\"")
    }
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
    implementation(libs.core.ktx)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.service)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.play.services.wearable)
    implementation(libs.coroutines.android)
    implementation(libs.coroutines.play.services)
}
