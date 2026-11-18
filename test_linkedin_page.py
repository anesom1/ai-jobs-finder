import requests
from bs4 import BeautifulSoup

url = "https://www.linkedin.com/jobs/view/4485938881/"

response = requests.get(
    url,
    headers={
        "User-Agent": "Mozilla/5.0"
    },
    timeout=30
)

print("Status:", response.status_code)
print("Final URL:", response.url)

soup = BeautifulSoup(response.text, "html.parser")

print("\nPage title:")
print(soup.title.get_text(strip=True) if soup.title else "Not found")

print("\nCompany metadata:")
for tag in soup.find_all("meta"):
    if tag.get("property") in [
        "og:title",
        "og:description",
        "og:url"
    ]:
        print(tag.get("property"), ":", tag.get("content"))

print("\nDescription element:")

element = soup.find(
    "div",
    class_="show-more-less-html__markup"
)

if element:
    description = element.get_text("\n", strip=True)
    print(description[:1000])
else:
    print("Description element not found.")