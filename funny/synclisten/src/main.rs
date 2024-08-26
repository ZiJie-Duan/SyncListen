use cpal::traits::{DeviceTrait, HostTrait};
use cpal::{StreamConfig, SupportedStreamConfig};
use std::fmt::write;
use std::process::exit;
use std::thread;
use std::time::Duration;

fn sound_map(x: f32) -> f32 {
    let a = 0.5;
    let b = 1.2;
    let c = 0.2;
    let d = 50.0;
    let e: f32 = 2.718281828459045;
    return b / (a + e.powf(-1.0 * (x - d))) + c;
}

fn main() {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .expect("no output device available");

    let mut supported_configs_range = device
        .supported_input_configs()
        .expect("error while querying configs");
    let supported_config = supported_configs_range
        .next()
        .expect("no supported config?!")
        .with_max_sample_rate();

    let stream_config = StreamConfig::from(supported_config);
    println!("dev: channel: {}", stream_config.channels);
    println!("syp: rate   : {}", stream_config.sample_rate.0.to_string());
    //println!("dev: buffsize: {}", stream_config.buffer_size.to_string());

    let stream = device.build_input_stream(
        &stream_config,
        move |data: &[f32], _: &cpal::InputCallbackInfo| {
            // react to stream events and read or write stream data here.
            let mut acc = 0.;
            let db = data.iter().for_each(|&v| acc += v.abs());
            let stn = acc as i32;
            let stn = (stn / 2) as i32;
            if stn > 1 {
                for i in 0..stn {
                    print!("*");
                    if i > 115 {
                        break;
                    }
                }
            }
            println!("");
        },
        move |err| {
            // react to errors here.
        },
        None, // None=blocking, Some(Duration)=timeout
    );
    println!("Hello, world!");
    thread::sleep(Duration::from_secs(200));
}
