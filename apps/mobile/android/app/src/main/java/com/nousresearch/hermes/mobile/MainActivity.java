package com.nousresearch.hermes.mobile;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
  @Override
  public void onCreate(android.os.Bundle savedInstanceState) {
    registerPlugin(FoldWindowPlugin.class);
    registerPlugin(MobileKeyboardPlugin.class);
    registerPlugin(MobileSecretsPlugin.class);
    super.onCreate(savedInstanceState);
  }
}
