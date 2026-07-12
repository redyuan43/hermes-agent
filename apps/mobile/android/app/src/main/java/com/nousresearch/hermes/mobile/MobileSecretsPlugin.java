package com.nousresearch.hermes.mobile;

import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

@CapacitorPlugin(name = "MobileSecrets")
public class MobileSecretsPlugin extends Plugin {
  private static final String KEY_ALIAS = "hermes_mobile_secrets_v1";
  private static final String PREFS_NAME = "hermes_mobile_secrets";

  @PluginMethod
  public void get(PluginCall call) {
    String key = call.getString("key");
    if (key == null || key.isEmpty()) {
      call.reject("A secret key is required.");
      return;
    }

    try {
      String encoded = preferences().getString(key, null);
      JSObject result = new JSObject();
      result.put("value", encoded == null ? null : decrypt(encoded));
      call.resolve(result);
    } catch (Exception error) {
      call.reject("Could not read the secure mobile secret.", error);
    }
  }

  @PluginMethod
  public void set(PluginCall call) {
    String key = call.getString("key");
    String value = call.getString("value");
    if (key == null || key.isEmpty() || value == null) {
      call.reject("A secret key and value are required.");
      return;
    }

    try {
      preferences().edit().putString(key, encrypt(value)).apply();
      call.resolve();
    } catch (Exception error) {
      call.reject("Could not store the secure mobile secret.", error);
    }
  }

  @PluginMethod
  public void remove(PluginCall call) {
    String key = call.getString("key");
    if (key == null || key.isEmpty()) {
      call.reject("A secret key is required.");
      return;
    }

    preferences().edit().remove(key).apply();
    call.resolve();
  }

  private SharedPreferences preferences() {
    return getContext().getSharedPreferences(PREFS_NAME, android.content.Context.MODE_PRIVATE);
  }

  private SecretKey secretKey() throws Exception {
    KeyStore store = KeyStore.getInstance("AndroidKeyStore");
    store.load(null);
    if (store.containsAlias(KEY_ALIAS)) {
      return ((KeyStore.SecretKeyEntry) store.getEntry(KEY_ALIAS, null)).getSecretKey();
    }

    KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
    generator.init(new KeyGenParameterSpec.Builder(
      KEY_ALIAS,
      KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
    )
      .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
      .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
      .build());
    return generator.generateKey();
  }

  private String encrypt(String value) throws Exception {
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(Cipher.ENCRYPT_MODE, secretKey());
    byte[] iv = cipher.getIV();
    byte[] ciphertext = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
    ByteBuffer payload = ByteBuffer.allocate(1 + iv.length + ciphertext.length);
    payload.put((byte) iv.length);
    payload.put(iv);
    payload.put(ciphertext);
    return Base64.encodeToString(payload.array(), Base64.NO_WRAP);
  }

  private String decrypt(String encoded) throws Exception {
    ByteBuffer payload = ByteBuffer.wrap(Base64.decode(encoded, Base64.NO_WRAP));
    int ivLength = Byte.toUnsignedInt(payload.get());
    byte[] iv = new byte[ivLength];
    payload.get(iv);
    byte[] ciphertext = new byte[payload.remaining()];
    payload.get(ciphertext);
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(128, iv));
    return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
  }
}
