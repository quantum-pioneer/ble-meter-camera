#pragma once
struct SPIClass { int sck=-1,miso=-1,mosi=-1,cs=-1; void begin(int a,int b,int c,int d){sck=a;miso=b;mosi=c;cs=d;} };
inline SPIClass SPI;
