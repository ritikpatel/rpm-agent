package rpm.edge

import org.eclipse.paho.client.mqttv3.IMqttDeliveryToken
import org.eclipse.paho.client.mqttv3.MqttAsyncClient
import org.eclipse.paho.client.mqttv3.MqttCallbackExtended
import org.eclipse.paho.client.mqttv3.MqttConnectOptions
import org.eclipse.paho.client.mqttv3.MqttMessage
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.ByteArrayInputStream
import java.io.File
import java.security.KeyStore
import java.security.cert.CertificateFactory
import javax.net.SocketFactory
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory

/** Anything that can accept an uplink publish (MQTT client, or the watch's Data Layer link to the phone). */
interface Transport {
    val connected: Boolean
    fun publish(topic: String, payload: String): Boolean
}

/**
 * MQTT 3.1.1 to AWS IoT Core (mutual TLS, client id == thing name, as the IoT policy requires) or to the local
 * Mosquitto broker. Durability is the outbox's job, so Paho keeps nothing on disk.
 */
class MqttTransport(
    serverUri: String,
    private val clientId: String,
    private val socketFactory: SocketFactory? = null,
    private val onCommand: (String) -> Unit = {},
    private val log: (String) -> Unit = {},
) : Transport {
    private val client = MqttAsyncClient(serverUri, clientId, MemoryPersistence())

    override val connected: Boolean get() = client.isConnected

    fun start() {
        client.setCallback(object : MqttCallbackExtended {
            override fun connectComplete(reconnect: Boolean, serverURI: String) {
                log("mqtt connected to $serverURI (reconnect=$reconnect)")
                runCatching { client.subscribe(Topics.commands(clientId), 1) }.onFailure { log("subscribe failed: $it") }
            }
            override fun connectionLost(cause: Throwable?) = log("mqtt connection lost: ${cause?.message}")
            override fun messageArrived(topic: String, message: MqttMessage) = onCommand(String(message.payload))
            override fun deliveryComplete(token: IMqttDeliveryToken?) = Unit
        })
        val options = MqttConnectOptions().apply {
            isAutomaticReconnect = true
            isCleanSession = true
            keepAliveInterval = 30
            connectionTimeout = 10
            maxInflight = 50
            mqttVersion = MqttConnectOptions.MQTT_VERSION_3_1_1
            this@MqttTransport.socketFactory?.let { socketFactory = it }
        }
        runCatching { client.connect(options) }.onFailure { log("mqtt connect failed: ${it.message}") }
    }

    override fun publish(topic: String, payload: String): Boolean {
        if (!client.isConnected) return false
        return runCatching {
            client.publish(topic, payload.toByteArray(), 1, false).waitForCompletion(5_000)
            true
        }.getOrElse { log("publish failed: ${it.message}"); false }
    }

    fun stop() { runCatching { client.disconnect().waitForCompletion(2_000) }; runCatching { client.close() } }
}

/**
 * Device identity written by edge/android/provision.sh into one directory: provisioning.json
 * ({"mqtt_uri", "device_id", "p12_password"}), device.p12 (certificate + private key) and ca.pem (the broker's CA:
 * Amazon Root CA 1 on AWS, the dev CA locally). An ssl:// endpoint without its key material is a hard error rather
 * than a silent fallback to an unauthenticated connection.
 */
data class Provisioning(val mqttUri: String, val deviceId: String, val socketFactory: SocketFactory?) {
    companion object {
        fun load(dir: File): Provisioning? {
            val file = File(dir, "provisioning.json").takeIf { it.exists() } ?: return null
            val o = RpmJson.parseToJsonElement(file.readText()).jsonObject
            fun field(k: String) = requireNotNull(o[k]?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }) {
                "provisioning.json: missing $k"
            }
            val uri = field("mqtt_uri")
            val tls = if (uri.startsWith("ssl://")) {
                val p12 = File(dir, "device.p12")
                val ca = File(dir, "ca.pem")
                require(p12.exists() && ca.exists()) { "$uri needs device.p12 and ca.pem next to provisioning.json" }
                Tls.socketFactory(p12.readBytes(), field("p12_password").toCharArray(), ca.readBytes())
            } else null
            return Provisioning(uri, field("device_id"), tls)
        }
    }
}

/** Mutual-TLS socket factory from a PKCS#12 device identity + the broker's CA (PEM). */
object Tls {
    fun socketFactory(p12: ByteArray, password: CharArray, caPem: ByteArray): SocketFactory {
        val keyStore = KeyStore.getInstance("PKCS12").apply { load(ByteArrayInputStream(p12), password) }
        val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(keyStore, password) }
        val trust = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null, null)
            CertificateFactory.getInstance("X.509").generateCertificates(ByteArrayInputStream(caPem))
                .forEachIndexed { i, c -> setCertificateEntry("ca$i", c) }
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(trust) }
        return SSLContext.getInstance("TLSv1.2").apply { init(kmf.keyManagers, tmf.trustManagers, null) }.socketFactory
    }
}
