use cpal::traits::{DeviceTrait, HostTrait};
use cpal::{StreamConfig, SupportedStreamConfig};
use std::fmt::write;
use std::process::exit;
use std::thread;
use std::time::Duration;

fn main() {
    let host = cpal::default_host();
    let in_device = host
        .default_input_device()
        .expect("no input device available");

    let out_device = host
        .default_input_device()
        .expect("no output device available");

    let mut supported_configs_range = in_device
        .supported_input_configs()
        .expect("error while querying configs");
    let in_supported_config = supported_configs_range
        .next()
        .expect("no supported config?!")
        .with_max_sample_rate();

    let mut supported_configs_range = out_device
        .supported_input_configs()
        .expect("error while querying configs");
    let out_supported_config = supported_configs_range
        .next()
        .expect("no supported config?!")
        .with_max_sample_rate();

    let in_stream_config = StreamConfig::from(in_supported_config);
    let stream = device.build_input_stream(
        &stream_config,
        move |data: &[f32], _: &cpal::InputCallbackInfo| {
            // react to stream events and read or write stream data here.
            let mut acc = 0.;
            let db = data.iter().for_each(|&v| acc += v);
            let stn = acc as i32;
            let stn = (stn / 5) as i32;
            for i in 0..stn {
                print!("*");
            }
            println!("");
        },
        move |err| {
            // react to errors here.
        },
        None, // None=blocking, Some(Duration)=timeout
    );
    println!("Hello, world!");
    thread::sleep(Duration::from_secs(2));
}
