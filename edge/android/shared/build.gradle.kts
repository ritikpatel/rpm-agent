plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

dependencies {
    api(libs.serialization.json)
    api(libs.coroutines.core)
    api(libs.paho.mqtt)
    testImplementation(libs.junit)
}

tasks.test {
    // contract examples live at the repo root so Kotlin and Python validate against the same files
    systemProperty("contracts.dir", rootProject.projectDir.resolve("../../contracts").absolutePath)
}
