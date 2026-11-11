<?php
/**
 * Plugin Name: Origin load guard
 * Description: Cap Action Scheduler ajax runners and Woo cart-fragment admin-ajax on non-cart pages.
 * Author: docker-multi-site-host
 */

if (!defined('ABSPATH')) {
	exit;
}

// Action Scheduler: system/wp-cron only — ajax runners saturate PHP-FPM under traffic
add_filter('action_scheduler_allow_async_request_runner', '__return_false');

add_filter('action_scheduler_queue_runner_concurrent_batches', static function () {
	return 1;
});

add_filter('action_scheduler_queue_runner_batch_size', static function () {
	return 10;
});

// Woo cart fragments: skip admin-ajax refresh outside cart/checkout
add_action('wp_enqueue_scripts', static function () {
	if (function_exists('is_cart') && (is_cart() || is_checkout())) {
		return;
	}
	wp_dequeue_script('wc-cart-fragments');
}, 100);
