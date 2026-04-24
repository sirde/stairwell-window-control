import os
import requests

API_KEY = os.environ["OPENWEATHER_API_KEY"]
LAT = float(os.environ.get("OPENWEATHER_LAT", "46.3833"))
LON = float(os.environ.get("OPENWEATHER_LON", "6.2333"))

url = f'https://api.openweathermap.org/data/2.5/weather?lat={LAT}&lon={LON}&appid={API_KEY}&units=metric'


def get_weather(lat, lon, api_key):
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx/5xx)
        data = response.json()

        # Check for necessary fields
        wind = data.get("wind", {})
        weather_list = data.get("weather", [])

        wind_speed = wind.get("gust", 0)
        is_raining = any(w.get("main") == "Rain" for w in weather_list)

        print(f"Wind speed: {wind_speed} m/s, Raining: {is_raining}")

        if wind_speed > 10 or is_raining:
            print("→ Close the windows!")
        else:
            print("→ Conditions are fine.")

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
    except requests.exceptions.ConnectionError:
        print("Connection error. Is your internet or the API reachable?")
    except requests.exceptions.Timeout:
        print("API request timed out.")
    except requests.exceptions.RequestException as e:
        print(f"Unexpected error: {e}")
    except Exception as e:
        print(f"Something else went wrong: {e}")


# Run check
get_weather(LAT, LON, API_KEY)