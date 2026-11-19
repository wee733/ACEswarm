package dev.aceswarm.h16;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.text.TextUtils;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.lang.ref.WeakReference;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

/** HTTP(S) pages have their own Android view with no native JavaScript interface. */
public class PageActivity extends Activity {
    static final String STORE_MODE = "storeMode";
    private static WeakReference<PageActivity> instance = new WeakReference<>(null);
    private WebView page;
    private static final int MAX_LIVE_PAGES = 3;
    private final Map<String, WebView> sessions = new LinkedHashMap<>(4, 0.75f, true);
    private TextView address;
    private ProgressBar progressBar;
    private FrameLayout browserArea;
    private ValueCallback<Uri[]> pendingFiles;

    static boolean isWebAddress(String text) {
        if (text == null) return false;
        try {
            java.net.URI uri = new java.net.URI(text);
            return ("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                && uri.getHost() != null && !WorkbenchActivity.HOST.equalsIgnoreCase(uri.getHost())
                && !"apps.aceswarm.invalid".equalsIgnoreCase(uri.getHost())
                && uri.getPort() != 0 && uri.getPort() <= 65535 && uri.getUserInfo() == null;
        } catch (java.net.URISyntaxException invalid) { return false; }
    }

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        instance = new WeakReference<>(this);
        String url = getIntent().getDataString();
        if (!isWebAddress(url)) { finish(); return; }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(android.view.Gravity.CENTER_VERTICAL);
        Button close = new Button(this);
        close.setText(R.string.desktop);
        close.setOnClickListener(view -> returnToDesktop());
        address = new TextView(this);
        address.setSingleLine(true);
        address.setEllipsize(TextUtils.TruncateAt.END);
        address.setText(url);
        Button reload = new Button(this);
        reload.setText(R.string.reload);
        reload.setOnClickListener(view -> { if (page != null) page.reload(); });
        bar.addView(close, -2, -2);
        bar.addView(address, new LinearLayout.LayoutParams(0, -2, 1));
        bar.addView(reload, -2, -2);
        layout.addView(bar, -1, -2);
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setVisibility(View.GONE);
        int barHeight = Math.max(2, (int) (3 * getResources().getDisplayMetrics().density));
        layout.addView(progressBar, new LinearLayout.LayoutParams(-1, barHeight));
        browserArea = new FrameLayout(this);
        layout.addView(browserArea, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(layout);
        WorkbenchActivity.applyFullscreen(this);
        long current = getSharedPreferences("browser", MODE_PRIVATE).getLong("generation", 0);
        selectPage(url, state != null && state.getLong("generation", 0) == current ? state : null);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String url = intent.getDataString();
        if (isWebAddress(url)) {
            setIntent(intent);
            selectPage(url, null);
        }
    }

    private void returnToDesktop() {
        startActivity(new Intent(this, WorkbenchActivity.class).addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
    }

    private void selectPage(String url, Bundle saved) {
        boolean storeMode = getIntent().getBooleanExtra(STORE_MODE, false);
        String storeOrigin = storeMode ? origin(Uri.parse(url)) : null;
        String sessionId = storeMode ? "store:" + storeOrigin : url;
        WebView selected = sessions.get(sessionId);
        if (page != null && page == selected) return;
        if (page != null) {
            page.onPause();
            browserArea.removeView(page);
        }
        if (selected == null) {
            if (sessions.size() == MAX_LIVE_PAGES) {
                Iterator<WebView> oldest = sessions.values().iterator();
                WorkbenchActivity.dispose(oldest.next());
                oldest.remove();
            }
            selected = newPage(storeOrigin);
            sessions.put(sessionId, selected);
            if (storeMode && !isStoreRoute(Uri.parse(url).getPath())) url = storeOrigin + "/";
            if (saved == null || selected.restoreState(saved) == null) selected.loadUrl(url);
        }
        page = selected;
        browserArea.addView(page, -1, -1);
        address.setText(page.getUrl() == null ? url : page.getUrl());
        updateProgress(page.getProgress());
        page.onResume();
    }

    private void updateProgress(int progress) {
        if (progressBar == null) return;
        if (progress <= 0 || progress >= 100) {
            progressBar.setVisibility(View.GONE);
        } else {
            progressBar.setProgress(progress);
            progressBar.setVisibility(View.VISIBLE);
        }
    }

    private static String origin(Uri uri) {
        String scheme = uri.getScheme().toLowerCase(java.util.Locale.ROOT);
        String host = uri.getHost().toLowerCase(java.util.Locale.ROOT);
        int port = uri.getPort();
        if (port == ("https".equals(scheme) ? 443 : 80)) port = -1;
        return scheme + "://" + host + (port == -1 ? "" : ":" + port);
    }

    private static boolean isStoreRoute(String path) {
        if (path == null || path.isEmpty() || "/".equals(path)) return true;
        String trimmed = path.endsWith("/") ? path.substring(0, path.length() - 1) : path;
        return "/login".equals(trimmed) || "/store".equals(trimmed)
            || "/me/new".equals(trimmed) || "/me/account".equals(trimmed)
            || trimmed.matches("/apps/[^/]+");
    }

    private WebResourceResponse storeAsset(String filename, String mime) {
        try {
            return new WebResourceResponse(mime, "UTF-8", getAssets().open("store/" + filename));
        } catch (IOException missing) {
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
        }
    }

    private WebView newPage(String storeOrigin) {
        WebView web = new WebView(this);
        WorkbenchActivity.configureWebView(web);
        web.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (storeOrigin == null || !"GET".equals(request.getMethod()) || !isWebAddress(uri.toString())
                        || !storeOrigin.equals(origin(uri))) return null;
                String path = uri.getPath();
                if (request.isForMainFrame() && isStoreRoute(path)) return storeAsset("index.html", "text/html");
                if ("/_aceswarm_store/workbench.js".equals(path)) return storeAsset("workbench.js", "application/javascript");
                if ("/_aceswarm_store/workbench.css".equals(path)) return storeAsset("workbench.css", "text/css");
                return null;
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !isWebAddress(request.getUrl().toString());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, String next) {
                return !isWebAddress(next);
            }
            @Override public void onPageStarted(WebView view, String next, android.graphics.Bitmap icon) {
                if (view == page) address.setText(next);
            }
            @Override public void doUpdateVisitedHistory(WebView view, String next, boolean isReload) {
                if (view == page && next != null) address.setText(next);
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();
                if (view == page) message(R.string.ssl_error);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int newProgress) {
                if (view == page) updateProgress(newProgress);
            }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                                        FileChooserParams params) {
                if (pendingFiles != null) pendingFiles.onReceiveValue(null);
                pendingFiles = callback;
                Intent intent = params.createIntent();
                if (intent == null) {
                    intent = new Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE);
                }
                String[] types = params.getAcceptTypes();
                if (types == null || types.length == 0 || types.length > 1
                        || (types[0] != null && (types[0].isEmpty() || types[0].startsWith(".")))) {
                    intent.setType("*/*");
                }
                if (params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                    intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                }
                try { startActivityForResult(intent, 1); }
                catch (ActivityNotFoundException unavailable) {
                    pendingFiles.onReceiveValue(null);
                    pendingFiles = null;
                }
                return true;
            }
        });
        web.setDownloadListener((target, agent, disposition, mime, size) -> {
            if (!isWebAddress(target)) { message(R.string.download_unsupported); return; }
            DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) { message(R.string.downloads_unavailable); return; }
            DownloadManager.Request download = new DownloadManager.Request(Uri.parse(target));
            download.setTitle(URLUtil.guessFileName(target, disposition, mime));
            if (mime != null && !mime.isEmpty()) download.setMimeType(mime);
            String cookie = CookieManager.getInstance().getCookie(target);
            if (cookie != null && !cookie.isEmpty()) download.addRequestHeader("Cookie", cookie);
            if (agent != null && !agent.isEmpty()) download.addRequestHeader("User-Agent", agent);
            download.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            try {
                manager.enqueue(download);
                message(R.string.download_started);
            } catch (RuntimeException unavailable) { message(R.string.downloads_unavailable); }
        });
        return web;
    }
    private void message(int text) { Toast.makeText(this, text, Toast.LENGTH_SHORT).show(); }
    @Override protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        state.putLong("generation", getSharedPreferences("browser", MODE_PRIVATE).getLong("generation", 0));
        if (page != null) page.saveState(state);
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == 1 && pendingFiles != null) {
            Uri[] uris = null;
            if (result == RESULT_OK && data != null) {
                ClipData clip = data.getClipData();
                if (clip != null && clip.getItemCount() > 0) {
                    uris = new Uri[clip.getItemCount()];
                    for (int i = 0; i < clip.getItemCount(); i++) uris[i] = clip.getItemAt(i).getUri();
                } else {
                    uris = WebChromeClient.FileChooserParams.parseResult(result, data);
                }
            }
            pendingFiles.onReceiveValue(uris);
            pendingFiles = null;
        }
    }
    @Override public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) WorkbenchActivity.applyFullscreen(this);
    }
    @Override public void onBackPressed() {
        if (page != null && page.canGoBack()) page.goBack(); else returnToDesktop();
    }
    @Override protected void onPause() { if (page != null) page.onPause(); super.onPause(); }
    @Override protected void onResume() {
        super.onResume();
        WorkbenchActivity.applyFullscreen(this);
        if (page != null) page.onResume();
    }
    static void clearSessions() {
        PageActivity activity = instance.get();
        if (activity == null) return;
        activity.disposePages();
        activity.finish();
    }
    private void disposePages() {
        if (pendingFiles != null) { pendingFiles.onReceiveValue(null); pendingFiles = null; }
        for (WebView session : sessions.values()) WorkbenchActivity.dispose(session);
        sessions.clear();
        page = null;
    }
    @Override protected void onDestroy() {
        if (instance.get() == this) instance.clear();
        disposePages();
        super.onDestroy();
    }
}
