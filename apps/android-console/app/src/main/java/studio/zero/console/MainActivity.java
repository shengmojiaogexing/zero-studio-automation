package studio.zero.console;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public class MainActivity extends Activity {
    private static final String PREFS_NAME = "zero_studio_console";
    private static final String KEY_SERVER_URL = "server_url";
    private static final int MENU_SERVER_SETTINGS = 1;
    private static final int FILE_CHOOSER_REQUEST = 42;

    private SharedPreferences preferences;
    private WebView webView;
    private ValueCallback<Uri[]> filePathCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        setTitle(R.string.app_name);

        String serverUrl = normalizeServerUrl(preferences.getString(KEY_SERVER_URL, ""));
        if (serverUrl.isEmpty()) {
            showServerSettings("");
        } else {
            openConsole(serverUrl);
        }
    }

    private String savedServerUrl() {
        return normalizeServerUrl(preferences.getString(KEY_SERVER_URL, ""));
    }

    private String normalizeServerUrl(String value) {
        String candidate = value == null ? "" : value.trim();
        if (candidate.isEmpty()) {
            return "";
        }
        if (!candidate.matches("(?i)^https?://.*")) {
            candidate = "http://" + candidate;
        }

        try {
            Uri uri = Uri.parse(candidate);
            String scheme = uri.getScheme();
            if (uri.getHost() == null || uri.getEncodedAuthority() == null
                    || !("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))) {
                return "";
            }

            String path = uri.getPath() == null ? "" : uri.getPath();
            while (path.endsWith("/") && !path.isEmpty()) {
                path = path.substring(0, path.length() - 1);
            }
            if (path.endsWith("/mobile")) {
                path = path.substring(0, path.length() - "/mobile".length());
            }

            String normalized = new Uri.Builder()
                    .scheme(scheme)
                    .encodedAuthority(uri.getEncodedAuthority())
                    .path(path)
                    .build()
                    .toString();
            while (normalized.endsWith("/")) {
                normalized = normalized.substring(0, normalized.length() - 1);
            }
            return normalized;
        } catch (RuntimeException ignored) {
            return "";
        }
    }

    private void showServerSettings(String errorMessage) {
        disposeWebView();

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER_HORIZONTAL);
        content.setPadding(dp(28), dp(40), dp(28), dp(32));
        content.setBackgroundColor(Color.rgb(11, 21, 21));

        TextView title = new TextView(this);
        title.setText("连接 ZERO STUDIO");
        title.setTextColor(Color.WHITE);
        title.setTextSize(28);
        title.setGravity(Gravity.CENTER);
        content.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView description = new TextView(this);
        description.setText("填写电脑端主控台地址。手机与电脑连接同一网络后，即可直接使用完整手机中控台。");
        description.setTextColor(Color.rgb(177, 196, 193));
        description.setTextSize(15);
        description.setPadding(0, dp(18), 0, dp(26));
        content.addView(description);

        EditText serverInput = new EditText(this);
        serverInput.setSingleLine(true);
        serverInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        serverInput.setHint("http://192.168.1.10:3080");
        serverInput.setTextColor(Color.WHITE);
        serverInput.setHintTextColor(Color.rgb(112, 137, 133));
        serverInput.setText(savedServerUrl());
        content.addView(serverInput, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView error = new TextView(this);
        error.setText(errorMessage);
        error.setTextColor(Color.rgb(255, 139, 128));
        error.setPadding(0, dp(10), 0, dp(8));
        content.addView(error);

        Button connect = new Button(this);
        connect.setText("保存并进入主控台");
        connect.setOnClickListener(view -> {
            String serverUrl = normalizeServerUrl(serverInput.getText().toString());
            if (serverUrl.isEmpty()) {
                error.setText("地址无效，请输入类似 http://192.168.1.10:3080 的地址");
                return;
            }
            preferences.edit().putString(KEY_SERVER_URL, serverUrl).apply();
            openConsole(serverUrl);
        });
        content.addView(connect, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView note = new TextView(this);
        note.setText("只需填写地址和端口，也可以粘贴完整的 /mobile 地址。\n以后打开 App 会自动进入；更换地址请使用右上角菜单。");
        note.setTextColor(Color.rgb(126, 151, 147));
        note.setTextSize(13);
        note.setPadding(0, dp(24), 0, 0);
        content.addView(note);

        ScrollView scrollView = new ScrollView(this);
        scrollView.setFillViewport(true);
        scrollView.addView(content);
        setContentView(scrollView);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void openConsole(String serverUrl) {
        disposeWebView();
        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(11, 21, 21));

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(false);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER_REQUEST);
                    return true;
                } catch (ActivityNotFoundException exception) {
                    filePathCallback = null;
                    return false;
                }
            }
        });

        setContentView(webView);
        webView.loadUrl(serverUrl + "/mobile");
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void disposeWebView() {
        if (filePathCallback != null) {
            filePathCallback.onReceiveValue(null);
            filePathCallback = null;
        }
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && filePathCallback != null) {
            Uri[] results = resultCode == RESULT_OK
                    ? WebChromeClient.FileChooserParams.parseResult(resultCode, data) : null;
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, MENU_SERVER_SETTINGS, 0, "服务器设置")
                .setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == MENU_SERVER_SETTINGS) {
            showServerSettings("");
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        disposeWebView();
        super.onDestroy();
    }
}
