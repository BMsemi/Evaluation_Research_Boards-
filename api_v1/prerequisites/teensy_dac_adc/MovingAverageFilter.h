#pragma once

class MovingAverageFilter {
public:
  explicit MovingAverageFilter(int) {}

  float process(float value) { return value; }
};
