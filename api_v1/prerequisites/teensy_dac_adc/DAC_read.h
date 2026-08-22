#include <Arduino.h>
#include <MovingAverageFilter.h>
#include "MedianFilterLib2.h"
#include <SPI.h>
#include "ads12xx.h"


//FILTERS
MedianFilter2<float> medianFilter(40);
MovingAverageFilter movingAverageFilter(40);
const int samples=50;

//DAC
#define NUM_BYTES 3 //Number of bytes in SPI frame
//dac hardware control
uint8_t initTx0[NUM_BYTES];
int8_t ret;
uint8_t *initPtr;//Range: +/- 2.5V
byte dac[16] = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19,
                0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F};
byte dac_all = 0x0F;

//
#define SPI1_CHECK
const int   slaveSelectPin = 2,
            slaveSelectPin2 = 3,
            ldacPin = 6,
            spiSckPin = 27,
            spiMosiPin = 26;
SPISettings settingsA(4000000, MSBFIRST, SPI_MODE1); 
int8_t DAC1transfer(uint8_t* datapointer, uint8_t numBytes);
int8_t DAC2transfer(uint8_t* datapointer, uint8_t numBytes);

void init_communication(){ 
  Serial.begin(115200);
  SPI1.beginTransaction(settingsA);  
  pinMode(spiSckPin, OUTPUT); // SPI_SCK pin
  pinMode(spiMosiPin, OUTPUT);  // SPI_MOSI pin  
  pinMode(slaveSelectPin, OUTPUT); // Configure PD2 as CLR
  pinMode(slaveSelectPin2, OUTPUT);
  #ifdef SPI1_CHECK
  SPI1.begin();
  SPI1.setMOSI(spiMosiPin); SPI1.setSCK(spiSckPin); //SPI1.setCS(slaveSelectPin);
  #endif
  digitalWrite(slaveSelectPin2, HIGH); // set CLR HIGH by default
  digitalWrite(slaveSelectPin, HIGH); // set CLR HIGH by default
}
  
void init_dac()
{   

  //dac configs

  initTx0[0] = 0x0A; 
  initTx0[1] = 0x01; // 0 = 5V, 1 = 10V, 2 = 20V, 4 = 40V
  initTx0[2] = 0x01; // 9 = +/- 5V, A = +/- 10V, C = +/- 20V, D = +/- 2V5
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES); 
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);                                                         

  // Range: +10V
  initTx0[0] = 0x0B; //ADDRESS
  initTx0[1] = 0x01; //DATA
  initTx0[2] = 0x01; //DATA
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  // Range: +10V
  initTx0[0] = 0x0C;
  initTx0[1] = 0x01;
  initTx0[2] = 0x01;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  // Range: +10V
  initTx0[0] = 0x0D;
  initTx0[1] = 0x01;
  initTx0[2] = 0x01;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  // DAC1 channels 12..15 use the 0..5V span; DAC13 is VDDa1 and DAC15 is VDDc2.
  initTx0[0] = 0x0D;
  initTx0[1] = 0x00;
  initTx0[2] = 0x00;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  //SPI ConfigurationRegister
  initTx0[0] = 0x03;
  initTx0[1] = 0x00;// 0x0A;
  initTx0[2] = 0x04;// 0x84;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  // Re-assert DAC1 channels 12..15 span after the configuration register write.
  initTx0[0] = 0x0D;
  initTx0[1] = 0x00;
  initTx0[2] = 0x00;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  //Power-up all channels
  initTx0[0] = 0x09;
  initTx0[1] = 0b00000000;//dac15 to 8
  initTx0[2] = 0b00000000;//dac7 to 0 
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  initTx0[0] = 0x09;
  initTx0[1] = 0b00000000;//dac15 to 8 
  initTx0[2] = 0b00000000;//dac7 to 0 
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  //Enable Broadcast for all Channels
  initTx0[0] = 0x05;
  initTx0[1] = 0xFF;
  initTx0[2] = 0xFF;
  initPtr = &initTx0[0];
  ret = DAC1transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);
  ret = DAC2transfer((uint8_t*)initPtr, (uint8_t)NUM_BYTES);

  //threads.setSliceMicros(2);
  }
  
int8_t DAC1transfer(uint8_t* datapointer, uint8_t numBytes=3) {
    // unsigned long rxtxData[MAX_BYTES];
    unsigned long rxtxData[3];
    /*if(numBytes > MAX_BYTES){
    return -1;
    }
    */
    if(datapointer == NULL){
        return -1;
        
    }

    for(int byteCount=0; byteCount < numBytes; byteCount++) {
        rxtxData[byteCount] = datapointer[byteCount];
        }

    digitalWrite(slaveSelectPin, LOW);
    for(int byteCount=0; byteCount < numBytes; byteCount++){
        SPI1.transfer((uint8_t) rxtxData[byteCount]);
      }
      
      digitalWrite(slaveSelectPin, HIGH);
      return (int8_t) 0;
}

int8_t DAC2transfer(uint8_t* datapointer, uint8_t numBytes=3) {
    // unsigned long rxtxData[MAX_BYTES];
    unsigned long rxtxData[3];
    /*if(numBytes > MAX_BYTES){
    return -1;
    }
    */
    if(datapointer == NULL){
        return -1;
        
    }

    for(int byteCount=0; byteCount < numBytes; byteCount++) {
        rxtxData[byteCount] = datapointer[byteCount];
        }

    digitalWrite(slaveSelectPin2, LOW);
    for(int byteCount=0; byteCount < numBytes; byteCount++){
        SPI1.transfer((uint8_t) rxtxData[byteCount]);
      }
      
      digitalWrite(slaveSelectPin2, HIGH);
      return (int8_t) 0;
}
////////////////////////////////////////////////////////////
