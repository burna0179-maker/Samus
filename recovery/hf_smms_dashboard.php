<?php
/**
 * Plugin Name: HF SMMS Dashboard
 * Description: HustleForge SMMS control panel — health monitoring + deployment actions
 * Version: 1.0.0
 * Author: HustleForge
 *
 * Source: ChatGPT recovery chat 38
 *
 * Canonical relationship:
 *   [NEW] WordPress operator surface for SMMS Global Automation Project
 *   [PAIRS WITH] smms_global_automation_project.md (umbrella spec)
 *   [EXPANDS §6 application] secondary UI surface (in addition to phone ops gateway)
 *
 * Endpoints consumed:
 *   GET  /v1/health/full
 *   POST /v1/deploy/profile/{daily_cycle|social_suite|market_sweep|auto_repair}
 *   POST /api/chat
 */

if ( ! defined( 'ABSPATH' ) ) { exit; }

class HF_SMMS_Dashboard {
    const OPTION_BASE_URL  = 'hf_smms_base_url';
    const OPTION_API_TOKEN = 'hf_smms_api_token';
    const NONCE_ACTION     = 'hf_smms_dashboard_nonce';

    public function __construct() {
        add_action( 'admin_menu', [ $this, 'register_menu' ] );
        add_action( 'admin_init', [ $this, 'register_settings' ] );
        add_action( 'admin_enqueue_scripts', [ $this, 'enqueue_assets' ] );
        add_action( 'wp_ajax_hf_smms_health', [ $this, 'ajax_health' ] );
        add_action( 'wp_ajax_hf_smms_deploy', [ $this, 'ajax_deploy' ] );
    }

    public function register_menu() {
        add_menu_page(
            'HF SMMS Dashboard', 'HF SMMS', 'manage_options',
            'hf-smms-dashboard', [ $this, 'render_dashboard_page' ],
            'dashicons-admin-site-alt3', 3
        );
        add_submenu_page(
            'hf-smms-dashboard', 'HF SMMS Settings', 'Settings',
            'manage_options', 'hf-smms-dashboard-settings',
            [ $this, 'render_settings_page' ]
        );
    }

    public function register_settings() {
        register_setting( 'hf_smms_settings_group', self::OPTION_BASE_URL,
            [ 'sanitize_callback' => 'esc_url_raw', 'default' => 'http://127.0.0.1:8080' ] );
        register_setting( 'hf_smms_settings_group', self::OPTION_API_TOKEN,
            [ 'sanitize_callback' => 'sanitize_text_field', 'default' => '' ] );

        add_settings_section( 'hf_smms_main_section', 'HF SMMS Connection',
            function () { echo '<p>Configure connection to your SMMS API.</p>'; },
            'hf-smms-dashboard-settings' );

        add_settings_field( self::OPTION_BASE_URL, 'SMMS Base URL',
            [ $this, 'field_base_url' ], 'hf-smms-dashboard-settings', 'hf_smms_main_section' );
        add_settings_field( self::OPTION_API_TOKEN, 'SMMS API Token',
            [ $this, 'field_api_token' ], 'hf-smms-dashboard-settings', 'hf_smms_main_section' );
    }

    public function field_base_url() {
        $value = esc_attr( get_option( self::OPTION_BASE_URL, 'http://127.0.0.1:8080' ) );
        echo '<input type="text" class="regular-text" name="' . esc_attr( self::OPTION_BASE_URL ) . '" value="' . $value . '" />';
    }

    public function field_api_token() {
        $value = esc_attr( get_option( self::OPTION_API_TOKEN, '' ) );
        echo '<input type="password" class="regular-text" name="' . esc_attr( self::OPTION_API_TOKEN ) . '" value="' . $value . '" />';
    }

    public function enqueue_assets( $hook ) {
        if ( $hook !== 'toplevel_page_hf-smms-dashboard' ) { return; }
        wp_register_script( 'hf-smms-dashboard-js', '', [ 'jquery' ], '1.0.0', true );
        wp_enqueue_script( 'hf-smms-dashboard-js' );
        wp_localize_script( 'hf-smms-dashboard-js', 'HF_SMMS_DASHBOARD', [
            'ajax_url' => admin_url( 'admin-ajax.php' ),
            'nonce'    => wp_create_nonce( self::NONCE_ACTION ),
        ] );
    }

    public function render_dashboard_page() {
        if ( ! current_user_can( 'manage_options' ) ) { return; }
        $base_url = esc_html( get_option( self::OPTION_BASE_URL, 'http://127.0.0.1:8080' ) );
        ?>
        <div class="wrap hf-smms-dashboard">
            <h1>HF SMMS Dashboard</h1>
            <p><strong>Base URL:</strong> <?php echo $base_url; ?></p>
            <div class="hf-smms-grid">
                <div class="hf-smms-card">
                    <h2>System Health</h2>
                    <button class="button button-primary" id="hf-smms-refresh-health">Refresh Health</button>
                    <pre id="hf-smms-health-output">Click "Refresh Health" to query /v1/health/full.</pre>
                </div>
                <div class="hf-smms-card">
                    <h2>Deployment Actions</h2>
                    <button class="button button-secondary hf-smms-deploy" data-profile="daily_cycle">Run Daily Cycle</button>
                    <button class="button button-secondary hf-smms-deploy" data-profile="social_suite">Run Social Suite</button>
                    <pre id="hf-smms-deploy-output"></pre>
                </div>
                <div class="hf-smms-card">
                    <h2>Chat Test</h2>
                    <input type="text" id="hf-smms-chat-input" class="regular-text" placeholder="Test message" />
                    <button class="button" id="hf-smms-chat-send">Send</button>
                    <pre id="hf-smms-chat-output"></pre>
                </div>
            </div>
        </div>
        <?php
    }

    public function render_settings_page() {
        if ( ! current_user_can( 'manage_options' ) ) { return; }
        ?>
        <div class="wrap">
            <h1>HF SMMS Settings</h1>
            <form method="post" action="options.php">
                <?php settings_fields( 'hf_smms_settings_group' );
                do_settings_sections( 'hf-smms-dashboard-settings' );
                submit_button(); ?>
            </form>
        </div>
        <?php
    }

    public function ajax_health() {
        check_ajax_referer( self::NONCE_ACTION, 'nonce' );
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_send_json_error( [ 'message' => 'Unauthorized' ], 403 );
        }
        $base_url = rtrim( get_option( self::OPTION_BASE_URL, '' ), '/' );
        if ( empty( $base_url ) ) {
            wp_send_json_error( [ 'message' => 'Base URL not configured' ], 400 );
        }
        $response = wp_remote_get( $base_url . '/v1/health/full', [
            'timeout' => 10, 'headers' => $this->build_auth_headers(),
        ] );
        if ( is_wp_error( $response ) ) {
            wp_send_json_error( [ 'message' => $response->get_error_message() ], 500 );
        }
        wp_send_json_success( [
            'status_code' => wp_remote_retrieve_response_code( $response ),
            'json' => json_decode( wp_remote_retrieve_body( $response ), true ),
        ] );
    }

    public function ajax_deploy() {
        check_ajax_referer( self::NONCE_ACTION, 'nonce' );
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_send_json_error( [ 'message' => 'Unauthorized' ], 403 );
        }
        $profile = isset( $_POST['profile'] ) ? sanitize_text_field( wp_unslash( $_POST['profile'] ) ) : '';
        $endpoint_map = [
            'daily_cycle'  => '/v1/deploy/profile/daily_cycle',
            'social_suite' => '/v1/deploy/profile/social_suite',
            'market_sweep' => '/v1/deploy/profile/market_sweep',
            'auto_repair'  => '/v1/deploy/profile/auto_repair',
        ];
        if ( ! isset( $endpoint_map[ $profile ] ) ) {
            wp_send_json_error( [ 'message' => 'Unknown profile' ], 400 );
        }
        $base_url = rtrim( get_option( self::OPTION_BASE_URL, '' ), '/' );
        $headers = $this->build_auth_headers();
        $headers['Content-Type'] = 'application/json';
        $response = wp_remote_post( $base_url . $endpoint_map[ $profile ], [
            'timeout' => 15, 'headers' => $headers,
            'body' => wp_json_encode( [ 'profile' => $profile ] ),
        ] );
        if ( is_wp_error( $response ) ) {
            wp_send_json_error( [ 'message' => $response->get_error_message() ], 500 );
        }
        wp_send_json_success( [
            'status_code' => wp_remote_retrieve_response_code( $response ),
            'json' => json_decode( wp_remote_retrieve_body( $response ), true ),
        ] );
    }

    private function build_auth_headers() {
        $headers = [];
        $token = trim( get_option( self::OPTION_API_TOKEN, '' ) );
        if ( $token !== '' ) {
            $headers['Authorization'] = 'Bearer ' . $token;
        }
        return $headers;
    }
}

new HF_SMMS_Dashboard();

// Inline dark-theme CSS (HustleForge palette)
add_action( 'admin_head', function () {
    $screen = get_current_screen();
    if ( ! $screen || $screen->id !== 'toplevel_page_hf-smms-dashboard' ) { return; }
    ?>
    <style>
        .hf-smms-dashboard .hf-smms-grid { display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            grid-gap: 16px; margin-top: 16px; }
        .hf-smms-dashboard .hf-smms-card { background: #111827; color: #e5e7eb;
            padding: 16px; border-radius: 10px; box-shadow: 0 0 0 1px #1f2937; }
        .hf-smms-dashboard .hf-smms-card h2 { margin-top: 0; color: #22d3ee; }
        .hf-smms-dashboard pre { background: #020617; color: #e5e7eb;
            padding: 8px; border-radius: 6px; max-height: 260px; overflow: auto; font-size: 12px; }
    </style>
    <?php
} );

// Inline JS for AJAX handlers
add_action( 'admin_footer', function () {
    $screen = get_current_screen();
    if ( ! $screen || $screen->id !== 'toplevel_page_hf-smms-dashboard' ) { return; }
    $base = esc_js( rtrim( get_option( HF_SMMS_Dashboard::OPTION_BASE_URL, 'http://127.0.0.1:8080' ), '/' ) );
    ?>
    <script>
    (function ($) {
        function show(t, d) { try { $(t).text(typeof d === 'string' ? d : JSON.stringify(d, null, 2)); } catch(e) {} }
        $('#hf-smms-refresh-health').on('click', function () {
            $('#hf-smms-health-output').text('Loading...');
            $.post(HF_SMMS_DASHBOARD.ajax_url, { action: 'hf_smms_health', nonce: HF_SMMS_DASHBOARD.nonce })
                .done(function (r) { show('#hf-smms-health-output', r.data || r); });
        });
        $('.hf-smms-deploy').on('click', function () {
            var p = $(this).data('profile');
            $('#hf-smms-deploy-output').text('Deploying ' + p + '...');
            $.post(HF_SMMS_DASHBOARD.ajax_url, { action: 'hf_smms_deploy', nonce: HF_SMMS_DASHBOARD.nonce, profile: p })
                .done(function (r) { show('#hf-smms-deploy-output', r.data || r); });
        });
        $('#hf-smms-chat-send').on('click', function () {
            var msg = $('#hf-smms-chat-input').val() || '';
            if (!msg) return $('#hf-smms-chat-output').text('Enter a message first.');
            $.ajax({ method: 'POST', url: '<?php echo $base; ?>/api/chat',
                contentType: 'application/json', dataType: 'json',
                data: JSON.stringify({ message: msg }) })
                .done(function (r) { show('#hf-smms-chat-output', r); });
        });
    })(jQuery);
    </script>
    <?php
} );
